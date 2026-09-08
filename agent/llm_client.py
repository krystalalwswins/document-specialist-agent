"""LLMClient: thin wrapper over the OpenAI-compatible client.

Centralizes model / base_url / api_key wiring and is injectable so the
planner and executor can be unit-tested with a fake LLM.
"""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING
import time
from agent.runtime import current_run, check_budget



if TYPE_CHECKING:
    from core.config import Settings


class LLMClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        client: Optional[Any] = None,
    ) -> None:
        if settings is None:
            from core.config import get_settings
            settings = get_settings()
        self._settings = settings
        self._client = client

    @property
    def model(self) -> str:
        return self._settings.llm_model

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._settings.llm_api_key,
                base_url=self._settings.llm_base_url,
                timeout=self._settings.llm_timeout_seconds,
                max_retries=0,
            )
        return self._client

    def chat(self, messages: list[dict[str, Any]], tools=None, tool_choice=None) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        check_budget()
        started = time.monotonic()
        response = None
        error = None
        context = current_run.get()
        if context:
            kwargs['timeout'] = max(0.01, min(self._settings.llm_timeout_seconds, context.deadline - started))
        try:
            response = self._ensure_client().chat.completions.create(**kwargs)
            check_budget()
            return response
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            if context:
                usage = getattr(response, 'usage', None)
                context.manager.add_metric_events(context.task_id, 'llm_events', [{
                    'model': self.model, 'duration_ms': int((time.monotonic() - started) * 1000),
                    'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                    'completion_tokens': getattr(usage, 'completion_tokens', None),
                    'total_tokens': getattr(usage, 'total_tokens', None), 'error': error,
                }])
