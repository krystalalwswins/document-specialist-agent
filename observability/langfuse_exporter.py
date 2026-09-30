"""Durable, opt-in OTLP/HTTP JSON export of completed local observations.

One exporter process per TraceStore (same constraint as the local task store).
Network I/O never holds TraceStore.lock. Ambiguous delivery is NOT retried:
Langfuse v4 does not guarantee deduplication by span ID.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .store import encode, redact

logger = logging.getLogger(__name__)
_EXPORT_LOCK = threading.Lock()


class RetryLater(Exception):
    """Explicit server backpressure; the request was rejected."""


class Rejected(Exception):
    """Permanent HTTP rejection; do not retry automatically."""


class UncertainDelivery(Exception):
    """May have been accepted, including partial acceptance."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward project credentials to a redirect target.


def nanos(value):
    delta = datetime.fromisoformat(value).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str((delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000)


def attribute(key, value):
    return {"key": key, "value": {"stringValue": value if isinstance(value, str) else encode(value).decode()}}


class LangfuseExporter:
    def __init__(self, store, settings, *, transport=None):
        self.store, self.settings = store, settings
        self.transport = transport or self._send
        self.stop_event = threading.Event()
        self.thread = None
        self.disabled_reason = None
        self.endpoint = settings.langfuse_base_url.rstrip("/") + "/api/public/otel/v1/traces"
        self.public = settings.langfuse_public_key.get_secret_value()
        self.secret = settings.langfuse_secret_key.get_secret_value()
        parsed = urlsplit(settings.langfuse_base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not settings.trace_enabled:
            self.disabled_reason = "local_trace_disabled"
        elif not self.public or not self.secret:
            self.disabled_reason = "missing_project_keys"
        elif (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
              or parsed.path not in {"", "/"} or not (parsed.scheme == "https" or local_http)):
            self.disabled_reason = "invalid_base_url_use_https_or_loopback_http"
        self.destination = hashlib.sha256(encode([settings.langfuse_base_url.rstrip("/"), self.public])).hexdigest()

    def scrub(self, value):
        keys = {k.lower().replace("-", "_") for k in self.settings.langfuse_redact_keys}
        def walk(v):
            if isinstance(v, dict):
                return {k: "[REDACTED]" if k.lower().replace("-", "_") in keys else walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, str):
                for secret in (self.public, self.secret):
                    if secret:
                        v = v.replace(secret, "[REDACTED]")
            return v
        return walk(redact(value))

    def payload(self, tid, task_id, payload):
        limit = self.settings.langfuse_payload_max_bytes
        if payload["mode"] == "inline":
            value = self.scrub(payload["value"])
            if len(encode(value)) <= limit:
                return value
            return {"omitted": "export_size_limit", "source": "local_trace_inline"}
        if payload["mode"] == "reference":
            if payload["size_bytes"] <= limit:
                return self.scrub(self.store.read_payload(tid, payload["ref"]))
            return {**payload, "omitted": "export_size_limit", "task_id": task_id,
                    "access": "authenticated local task trace payload endpoint"}
        return payload

    def span(self, data, start, end):
        tid, task_id = start["trace_id"], start["task_id"]
        meta = self.scrub({**start["metadata"], **end["metadata"]})
        attrs = {
            "langfuse.trace.name": "document-specialist.task",
            "langfuse.trace.metadata.task_id": task_id,
            "langfuse.environment": self.settings.langfuse_environment,
            "langfuse.observation.type": start["type"],
            "langfuse.observation.input": self.payload(tid, task_id, start["input"]),
            "langfuse.observation.output": self.payload(tid, task_id, end["output"]),
            "langfuse.observation.metadata.local_sequence": start["sequence"],
            "langfuse.observation.metadata.local_status": end["status"],
        }
        # Propagate release to each observation, even when the root input is offloaded.
        root_input = data["events"][0]["input"]
        if root_input["mode"] == "reference":
            root_value = self.store.read_payload(tid, root_input["ref"])
        else:
            root_value = root_input.get("value")
        if isinstance(root_value, dict) and root_value.get("code_version"):
            attrs["langfuse.release"] = root_value["code_version"]
        for key, value in meta.items():
            if value is not None:
                # Bound metadata too; oversized values remain in the local trace.
                attrs["langfuse.observation.metadata." + key] = (
                    value if len(encode(value)) <= self.settings.langfuse_payload_max_bytes
                    else {"omitted": "export_size_limit", "source": "local_metadata"})
        if start["type"] == "generation":
            if meta.get("model"):
                attrs["langfuse.observation.model.name"] = meta["model"]
            request = attrs["langfuse.observation.input"]
            if isinstance(request, dict):
                params = {k: v for k, v in request.items() if k in {
                    "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "tool_choice", "response_format"}}
                attrs["langfuse.observation.model.parameters"] = params
            usage = meta.get("usage") or {}
            mapped = {dst: usage[src] for src, dst in (("prompt_tokens", "input"),
                      ("completion_tokens", "output"), ("total_tokens", "total"))
                      if type(usage.get(src)) is int and usage[src] >= 0}
            # cache_tokens remains metadata: it is a subset of input, not extra usage.
            if mapped:
                attrs["langfuse.observation.usage_details"] = mapped
            if meta.get("estimated_cost_usd") is not None:
                attrs["langfuse.observation.cost_details"] = {"total": meta["estimated_cost_usd"]}
        failed = end["status"] != "SUCCESS"
        if failed:
            attrs["langfuse.observation.level"] = "ERROR"
            attrs["langfuse.observation.status_message"] = (end.get("error") or {}).get("type", end["status"])
        if end.get("error"):
            attrs["langfuse.observation.metadata.error"] = self.payload(tid, task_id, {"mode": "inline", "value": end["error"]})
        span = {"traceId": tid, "spanId": start["observation_id"], "name": start["name"],
                "kind": 1, "startTimeUnixNano": nanos(start["occurred_at"]),
                "endTimeUnixNano": nanos(end["occurred_at"]),
                "status": {"code": 2 if failed else 1},
                "attributes": [attribute(k, v) for k, v in self.scrub(attrs).items()]}
        if start["parent_id"]:
            span["parentSpanId"] = start["parent_id"]
        return span

    def _send(self, body):
        auth = base64.b64encode((self.public + ":" + self.secret).encode()).decode()
        request = Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": "Basic " + auth, "Content-Type": "application/json",
            "x-langfuse-ingestion-version": "4"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.settings.langfuse_export_timeout) as response:
                result = json.loads(response.read(65536) or b"{}")
                partial = result.get("partialSuccess", result.get("partial_success", {}))
                if int(partial.get("rejectedSpans", partial.get("rejected_spans", 0))) or partial.get("errorMessage") or partial.get("error_message"):
                    raise UncertainDelivery("partial_response")
        except HTTPError as exc:
            if exc.code == 429:
                raise RetryLater("rate_limited") from None
            if 400 <= exc.code < 500 and exc.code != 408:
                raise Rejected("http_" + str(exc.code)) from None
            raise UncertainDelivery("http_" + str(exc.code)) from None
        # Transport errors, timeouts, malformed responses handled as uncertain by caller.

    def state(self, tid):
        try:
            with self.store.lock:
                return json.loads(self.store._read(self.store.directory(tid) / "langfuse.json"))
        except FileNotFoundError:
            return {"status": "not_enrolled", "observations": {}}

    def _save(self, tid, state):
        with self.store.lock:
            self.store._atomic(self.store.directory(tid) / "langfuse.json", encode(state))

    def resolve(self, tid, oid, decision):
        """Explicit operator decision after checking remote observation presence."""
        with _EXPORT_LOCK:
            state = self.state(tid)
            receipt = state["observations"].get(oid)
            if state.get("destination", self.destination) != self.destination:
                raise ValueError("destination changed")
            if not receipt or receipt["status"] not in {"uncertain", "rejected", "exhausted"}:
                raise ValueError("observation does not need manual resolution")
            if decision not in {"retry", "accepted"}:
                raise ValueError("invalid decision")
            state["observations"][oid] = {
                "status": "retry" if decision == "retry" else "accepted",
                "attempts": 0, "operator_decision": decision,
                "previous_status": receipt["status"], "resolved_at": time.time()}
            self._save(tid, state)
            return state["observations"][oid]

    def drain_once(self):
        if not self.settings.langfuse_enabled or self.disabled_reason:
            return
        # Also serializes multiple app instances in this process. Multi-process is unsupported.
        with _EXPORT_LOCK:
            if not self.store.root.exists():
                return
            for path in sorted(self.store.root.iterdir()):
                if self.stop_event.is_set():
                    return
                if not re.fullmatch(r"[0-9a-f]{32}", path.name) or path.is_symlink():
                    continue
                try:
                    self._export_trace(path.name)
                except Exception as exc:
                    logger.warning("Langfuse local export failed trace=%s error_type=%s", path.name, type(exc).__name__)

    def _export_trace(self, tid):
        state = self.state(tid)
        if state.get("status") == "not_enrolled":
            return  # Turning export on never silently uploads historical traces.
        if state.get("destination", self.destination) != self.destination:
            return  # Never mix receipts for different projects.
        state["destination"] = self.destination
        data = self.store.get(tid)
        starts = {e["observation_id"]: e for e in data["events"] if e["event"] == "started"}
        for end in data["events"]:
            if end["event"] != "ended" or self.stop_event.is_set():
                continue
            oid = end["observation_id"]
            receipt = state["observations"].get(oid, {})
            if receipt.get("status") == "sending":
                receipt["status"] = "uncertain"
                self._save(tid, state)
            if receipt.get("status") in {"accepted", "uncertain", "rejected", "exhausted"}:
                continue
            if receipt.get("next_attempt_at", 0) > time.time():
                continue
            span = self.span(data, starts[oid], end)
            body = encode({"resourceSpans": [{"resource": {"attributes": [attribute("service.name", "document-specialist-agent")]},
                "scopeSpans": [{"scope": {"name": "document-specialist.local-trace", "version": "1"}, "spans": [span]}]}]})
            if len(body) > 2_000_000:
                state["observations"][oid] = {"status": "rejected", "reason": "span_size_limit"}
                self._save(tid, state)
                continue
            receipt = {"status": "sending", "attempts": receipt.get("attempts", 0) + 1}
            state["observations"][oid] = receipt
            self._save(tid, state)  # Durable intent BEFORE network; crash becomes uncertain.
            try:
                self.transport(body)
                receipt["status"] = "accepted"
            except RetryLater:
                receipt["status"] = "retry" if receipt["attempts"] < self.settings.langfuse_export_max_attempts else "exhausted"
                receipt["next_attempt_at"] = time.time() + min(300, 2 ** receipt["attempts"])
            except Rejected:
                receipt["status"] = "rejected"
            except Exception:
                receipt["status"] = "uncertain"
            self._save(tid, state)
            if receipt["status"] != "accepted":
                logger.warning("Langfuse export trace=%s observation=%s status=%s", tid, oid, receipt["status"])

    def start(self):
        if not self.settings.langfuse_enabled:
            return
        if self.disabled_reason:
            logger.warning("Langfuse export disabled reason=%s", self.disabled_reason)
            return
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def run():
            while not self.stop_event.is_set():
                try:
                    self.drain_once()
                except Exception as exc:
                    logger.warning("Langfuse exporter error_type=%s", type(exc).__name__)
                self.stop_event.wait(self.settings.langfuse_export_interval)
        self.thread = threading.Thread(target=run, name="langfuse-export", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=self.settings.langfuse_export_timeout + 1)
        # Pending work stays durable and resumes at next API startup.
