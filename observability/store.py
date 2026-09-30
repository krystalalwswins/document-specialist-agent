"""Single-process local traces. Atomic JSONL replacement favors crash safety.

The process-wide lock also serializes separate store instances in one process.
No multi-process writer support; no background recovery of live workers.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from threading import RLock
from .model import Payload, TraceEvent, TraceManifest, new_trace_id, new_observation_id


class TraceNotFound(LookupError):
    pass


class TraceCorruptError(ValueError):
    pass


_LOCK = RLock()
_SENSITIVE = re.compile(r"^(authorization|proxy_authorization|cookie|set_cookie|api_?key|x_api_token|access_token|refresh_token|password|secret|credential|signature)$", re.I)


def redact(value):
    """Baseline redaction, not a general PII detector. Run before every write."""
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SENSITIVE.match(str(k).replace("-", "_")) else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)([?&](?:x-amz-[\w-]+|token|api_key|signature)=)[^&\s]+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)((?:api[_-]?key|password|secret|access_token)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
        return value
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class TraceStore:
    def __init__(self, root, *, inline_bytes=16000):
        if type(inline_bytes) is not int or inline_bytes < 1:
            raise ValueError("inline_bytes must be positive")
        self.root = Path(root).resolve()
        self.inline_bytes = inline_bytes
        self.lock = _LOCK

    def directory(self, trace_id):
        if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id) or int(trace_id, 16) == 0:
            raise TraceNotFound("trace not found")
        path = self.root / trace_id
        if path.resolve().parent != self.root or path.is_symlink():
            raise TraceNotFound("trace not found")
        return path

    def _atomic(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("unsafe trace path")
        tmp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with tmp.open("xb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _read(self, path):
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise TraceCorruptError("unsafe trace path")
        return path.read_bytes()

    def create(self, task_id, value):
        with self.lock:
            try:
                self.for_task(task_id)
            except TraceNotFound:
                pass
            else:
                raise ValueError("task already has trace")
            manifest = TraceManifest(new_trace_id(), task_id, new_observation_id())
            directory = self.directory(manifest.trace_id)
            self._atomic(directory / "trace.json", encode(manifest.to_dict()))
            self.append(manifest.trace_id, observation_id=manifest.root_id, parent_id=None,
                        type="span", name="task", event="started", status="RUNNING", input=value)
            return manifest.trace_id

    def payload(self, trace_id, value):
        value = redact(value)
        content = encode(value)
        if len(content) <= self.inline_bytes:
            return Payload.inline(value)
        ref = secrets.token_hex(16)
        self._atomic(self.directory(trace_id) / "payloads" / (ref + ".json"), content)
        return Payload.reference(ref, hashlib.sha256(content).hexdigest(), len(content))

    def get(self, trace_id):
        with self.lock:
            directory = self.directory(trace_id)
            try:
                manifest = TraceManifest.from_dict(json.loads(self._read(directory / "trace.json")))
            except FileNotFoundError:
                raise TraceNotFound("trace not found") from None
            except (ValueError, TypeError, KeyError) as exc:
                raise TraceCorruptError("invalid trace manifest") from exc
            if manifest.trace_id != trace_id:
                raise TraceCorruptError("trace identity mismatch")
            try:
                events = [TraceEvent.from_dict(json.loads(line)).to_dict()
                          for line in self._read(directory / "events.jsonl").splitlines()]
                self._validate(manifest, events)
            except FileNotFoundError:
                events = []
            except (ValueError, TypeError, KeyError) as exc:
                raise TraceCorruptError("invalid trace events") from exc
            reasons = []
            try:
                reasons = json.loads(self._read(directory / "capture_errors.json"))
            except FileNotFoundError:
                pass
            opened = {e["observation_id"] for e in events if e["event"] == "started"}
            opened -= {e["observation_id"] for e in events if e["event"] == "ended"}
            interrupted = any(e["status"] == "INTERRUPTED" for e in events)
            status = "INCOMPLETE" if reasons or interrupted or not events else ("OPEN" if opened else "COMPLETE")
            return dict(manifest=manifest.to_dict(), events=events, capture_status=status,
                        capture_errors=reasons, open_observation_ids=sorted(opened))

    @staticmethod
    def _validate(manifest, events):
        starts, ended = {}, set()
        for seq, e in enumerate(events, 1):
            oid, parent = e["observation_id"], e["parent_id"]
            if e["sequence"] != seq or e["trace_id"] != manifest.trace_id or e["task_id"] != manifest.task_id:
                raise ValueError("event identity/order mismatch")
            if e["event"] == "started":
                if oid in starts or (parent is None and (oid != manifest.root_id or seq != 1)):
                    raise ValueError("duplicate or invalid root")
                if parent is not None and (parent not in starts or parent in ended):
                    raise ValueError("parent is not open")
                starts[oid] = e
            else:
                if oid not in starts or oid in ended:
                    raise ValueError("observation not open")
                if any(starts[oid][key] != e[key] for key in ("parent_id", "name", "type")):
                    raise ValueError("observation identity changed")
                if any(s["parent_id"] == oid and child not in ended for child, s in starts.items()):
                    raise ValueError("children still open")
                ended.add(oid)

    def append(self, trace_id, *, input=None, output=None, **fields):
        with self.lock:
            data = self.get(trace_id)
            manifest = TraceManifest.from_dict(data["manifest"])
            event = TraceEvent(trace_id=trace_id, task_id=manifest.task_id,
                sequence=len(data["events"])+1, input=self.payload(trace_id, input),
                output=self.payload(trace_id, output), **redact(fields)).to_dict()
            events = data["events"] + [event]
            self._validate(manifest, events)
            self._atomic(self.directory(trace_id) / "events.jsonl", b"".join(encode(e)+b"\n" for e in events))
            return event

    def mark_incomplete(self, trace_id, reason):
        with self.lock:
            data = self.get(trace_id)
            self._atomic(self.directory(trace_id) / "capture_errors.json",
                         encode(data["capture_errors"] + [redact(reason)]))

    def for_task(self, task_id):
        with self.lock:
            if self.root.exists():
                for path in sorted(self.root.iterdir()):
                    if path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name):
                        data = self.get(path.name)
                        if data["manifest"]["task_id"] == task_id:
                            return data
            raise TraceNotFound("task trace not found")

    def read_payload(self, trace_id, ref):
        with self.lock:
            data = self.get(trace_id)
            matches = [e[k] for e in data["events"] for k in ("input", "output")
                       if e[k].get("mode") == "reference" and e[k].get("ref") == ref]
            if not matches:
                raise TraceNotFound("payload not found")
            expected = matches[0]
            try:
                content = self._read(self.directory(trace_id) / "payloads" / (ref + ".json"))
                if len(content) != expected["size_bytes"] or hashlib.sha256(content).hexdigest() != expected["sha256"]:
                    raise TraceCorruptError("payload integrity mismatch")
                return json.loads(content)
            except FileNotFoundError as exc:
                raise TraceCorruptError("referenced payload missing") from exc
