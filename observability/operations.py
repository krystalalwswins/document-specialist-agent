"""Best-effort operation scopes and snapshots, without changing business results."""
from dataclasses import asdict, is_dataclass
from enum import Enum
from functools import wraps
import time
import inspect
from .generation import _current, bind_trace, reset_trace


def json_value(value):
    if hasattr(value, "to_dict"):
        return json_value(value.to_dict())
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def traced(name, *, kind="span", inputs=None, outputs=None):
    """Capture factories run inside failure isolation; input is saved before call."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            context = _current.get()
            if not context or not context[1] or not context[2]:
                return fn(*args, **kwargs)
            recorder, tid, parent = context
            def capture_args():
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                positional = tuple(bound.arguments[p.name] for p in signature.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
                return positional, kwargs
            normalized = recorder._safe(tid, capture_args)
            capture_a, capture_k = normalized if normalized is not None else (args, kwargs)
            oid = recorder._safe(tid, lambda: recorder.start(tid, parent, kind, name,
                json_value(inputs(capture_a, capture_k)) if inputs else None))
            token = bind_trace(recorder, tid, oid) if oid else None
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                if oid:
                    recorder.end(tid, oid, status="INTERRUPTED" if not isinstance(exc, Exception) else "ERROR",
                        error={"type": type(exc).__name__, "message": str(exc)},
                        metadata={"duration_ms": int((time.monotonic()-started)*1000)})
                raise
            else:
                if oid:
                    def finish():
                        output = json_value(outputs(result, capture_a, capture_k) if outputs else result)
                        ok = getattr(result, "success", getattr(result, "ok", True))
                        return recorder.end(tid, oid, output=output, status="SUCCESS" if ok else "ERROR",
                            metadata={"duration_ms": int((time.monotonic()-started)*1000)})
                    finished = recorder._safe(tid, finish)
                    if finished is None:
                        recorder.end(tid, oid, status="ERROR", error={"type": "CaptureError", "message": "output capture failed"})
                return result
            finally:
                if token is not None:
                    reset_trace(token)
        return wrapped
    return decorate


def snapshot(name, value):
    context = _current.get()
    if not context or not context[1] or not context[2]:
        return
    recorder, tid, parent = context
    def record():
        oid = recorder.start(tid, parent, "span", name, None)
        if oid:
            result = recorder._safe(tid, lambda: recorder.end(tid, oid, output=json_value(value() if callable(value) else value)))
            if result is None:
                recorder.end(tid, oid, status="ERROR", error={"type": "CaptureError"})
    recorder._safe(tid, record)
