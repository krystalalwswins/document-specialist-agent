"""Context-local model capture, shared by all phases through LLMClient."""
from contextvars import ContextVar
import hashlib
import logging
from .model import new_observation_id
from .store import encode, redact

logger = logging.getLogger(__name__)
_current = ContextVar("agent_trace", default=None)


def bind_trace(recorder, trace_id, root_id):
    return _current.set((recorder, trace_id, root_id))


def reset_trace(token):
    _current.reset(token)


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def visible_response(response):
    choices = []
    for choice in value(response, "choices", []) or []:
        message = value(choice, "message")
        calls = []
        for call in value(message, "tool_calls", []) or []:
            function = value(call, "function")
            calls.append({"id": value(call, "id"), "type": value(call, "type"),
                          "function": {"name": value(function, "name"), "arguments": value(function, "arguments")}})
        choices.append({"index": value(choice, "index"), "finish_reason": value(choice, "finish_reason"),
                        "message": {"role": value(message, "role"), "content": value(message, "content"),
                                    "refusal": value(message, "refusal"), "tool_calls": calls}})
    return {"id": value(response, "id"), "model": value(response, "model"), "choices": choices}


class GenerationCapture:
    def __init__(self, request, on_event, prompt_version, pricing=None):
        self.context = _current.get()
        if self.context and (not self.context[1] or not self.context[2]):
            self.context = None
        self.request = request
        self.pricing = pricing
        self.metadata = {**getattr(on_event, "trace_metadata", {}),
                         "logical_call_id": new_observation_id(), "prompt_version": prompt_version,
                         "model": request.get("model")}

    def start(self, attempt):
        if self.context is None:
            return None
        recorder, tid, root = self.context
        def action():
            # Hash the same redacted UTF-8 JSON representation persisted as input.
            fingerprint = hashlib.sha256(encode(redact(self.request))).hexdigest()
            return recorder.start(tid, root, "generation", "llm.chat", self.request,
                                  metadata={**self.metadata, "attempt": attempt, "request_hash": fingerprint})
        return recorder._safe(tid, action)

    def finish(self, observation_id, *, response=None, error=None, usage=None, duration_ms=0):
        if self.context is None or observation_id is None:
            return
        recorder, tid, _ = self.context
        def action():
            actual_model = value(response, "model") or self.request.get("model")
            cost = None
            if error is None and self.pricing and usage:
                price = self.pricing.price_for(actual_model)
                prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
                if price and type(prompt) is int and type(completion) is int and min(prompt, completion) >= 0:
                    cached = usage.get("cache_tokens", 0)
                    cached = cached if type(cached) is int and cached >= 0 else 0
                    cost = float(price.estimate(prompt_tokens=prompt, completion_tokens=completion,
                                                cache_tokens=min(cached, prompt)))
            recorder.end(tid, observation_id, status="ERROR" if error else "SUCCESS",
                output=visible_response(response) if response is not None else None,
                error={"type": type(error).__name__, "message": str(error)} if error else None,
                metadata={"model": actual_model, "usage": usage or None, "duration_ms": duration_ms,
                          "estimated_cost_usd": cost,
                          "pricing_version": self.pricing.version if self.pricing else None})
        recorder._safe(tid, action)
