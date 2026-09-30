"""Best-effort tracing facade. Capture failures never change business outcomes."""
import logging
from .model import new_observation_id

logger = logging.getLogger(__name__)


class TraceRecorder:
    def __init__(self, store, *, export_enabled=False):
        self.store = store
        self.export_enabled = export_enabled
        self.failures = 0

    def _safe(self, trace_id, action):
        try:
            return action()
        except Exception as exc:
            self.failures += 1
            # Do not log exception messages: they may contain uncaptured secrets.
            logger.error("Trace capture failed trace=%s error_type=%s", trace_id, type(exc).__name__)
            if trace_id:
                try:
                    self.store.mark_incomplete(trace_id, {"reason": "capture_failed", "error_type": type(exc).__name__})
                except Exception:
                    logger.error("Trace incomplete marker could not be persisted trace=%s", trace_id)
            return None

    def create(self, task_id, input):
        tid = self._safe(None, lambda: self.store.create(task_id, input))
        if tid and self.export_enabled:
            from .store import encode
            self._safe(tid, lambda: self.store._atomic(
                self.store.directory(tid) / "langfuse.json",
                encode({"version": 1, "observations": {}})))
        return tid

    def start(self, trace_id, parent_id, type, name, input=None, *, metadata=None):
        if trace_id is None:
            return None
        def action():
            oid = new_observation_id()
            self.store.append(trace_id, observation_id=oid, parent_id=parent_id,
                type=type, name=name, event="started", status="RUNNING", input=input,
                metadata=metadata or {})
            return oid
        return self._safe(trace_id, action)

    def end(self, trace_id, observation_id, *, output=None, status="SUCCESS", error=None, metadata=None):
        if trace_id is None or observation_id is None:
            return None
        def action():
            with self.store.lock:
                data = self.store.get(trace_id)
                start = next(e for e in data["events"] if e["observation_id"] == observation_id and e["event"] == "started")
                return self.store.append(trace_id, observation_id=observation_id,
                    parent_id=start["parent_id"], type=start["type"], name=start["name"],
                    event="ended", status=status, output=output, error=error,
                    metadata={**start["metadata"], **(metadata or {})})
        return self._safe(trace_id, action)

    def interrupt(self, trace_id):
        """Explicit recovery ONLY after caller establishes that its worker is dead."""
        def action():
            with self.store.lock:
                data = self.store.get(trace_id)
                opened = set(data["open_observation_ids"])
                # Reverse starts is child-before-parent even for nested operations.
                for e in reversed(data["events"]):
                    if e["event"] == "started" and e["observation_id"] in opened:
                        self.end(trace_id, e["observation_id"], status="INTERRUPTED",
                                 metadata={"synthetic": True, "reason": "worker_stopped"})
                return self.store.get(trace_id)
        return self._safe(trace_id, action)
