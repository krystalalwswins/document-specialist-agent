"""LLMClient: OpenAI-compatible client with an explicit timeout and bounded retry.

Centralizes model / base_url / api_key wiring, and keeps a single chat call from
hanging forever:

- every attempt is bounded by ``LLM_TIMEOUT`` seconds (the SDK default is 10
  minutes, which showed up as a task sitting in RUNNING with no progress);
- retries reuse the shared ``RetryPolicy``, so only transient/timeout failures
  are replayed, with exponential backoff + jitter;
- the SDK's own retry loop is disabled (``max_retries=0``) so this policy is the
  single source of truth and every attempt can be observed.

The client is injectable, so the planner and executor stay unit-testable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from openai import OpenAI

from core.config import Settings, get_settings
from retry.retry_policy import RetryPolicy, classify_exception

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], None]


class LLMClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        client: Optional[Any] = None,
        retry_policy: Optional[RetryPolicy] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=self._settings.llm_max_attempts,
            base_delay=self._settings.llm_retry_base_delay,
            max_delay=self._settings.llm_retry_max_delay,
        )
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._settings.llm_model

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = OpenAI(
                api_key=self._settings.llm_api_key,
                base_url=self._settings.llm_base_url,
                timeout=self._settings.llm_timeout,
                max_retries=0,  # self._retry_policy owns retries
            )
        return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools=None,
        tool_choice=None,
        on_event: Optional[EventHandler] = None,
    ) -> Any:
        """Run one chat completion, retrying transient failures within the budget.

        ``on_event`` receives one dict per attempt so callers can persist retry
        evidence next to the tool-level ``retry_events``. The last failure is
        re-raised unchanged, keeping error classification accurate upstream.
        """
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                response = self._ensure_client().chat.completions.create(**kwargs)
            except Exception as exc:
                error_type = classify_exception(exc)
                decision = self._retry_policy.decide(attempt, error_type)
                duration_ms = int((time.monotonic() - started) * 1000)
                self._emit(
                    on_event,
                    self._event(
                        attempt,
                        error_type.value,
                        str(exc),
                        decision.reason,
                        duration_ms,
                        "FAILED" if decision.final else "RETRYING",
                    ),
                )
                if not decision.should_retry:
                    raise
                logger.warning(
                    "LLM call failed (attempt %d/%d, %s): %s - retrying in %.2fs",
                    attempt,
                    self._retry_policy.max_attempts,
                    error_type.value,
                    exc,
                    decision.delay_seconds,
                )
                self._sleep(decision.delay_seconds)
                continue

            self._emit(
                on_event,
                self._event(
                    attempt, None, None, "success", int((time.monotonic() - started) * 1000), "SUCCESS"
                ),
            )
            return response

    def _event(
        self,
        attempt: int,
        error_type: Optional[str],
        error_message: Optional[str],
        retry_reason: str,
        duration_ms: int,
        final_status: str,
    ) -> dict[str, Any]:
        return {
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model,
            "attempt": attempt,
            "error_type": error_type,
            "error_message": error_message,
            "retry_reason": retry_reason,
            "duration_ms": duration_ms,
            "final_status": final_status,
        }

    @staticmethod
    def _emit(on_event: Optional[EventHandler], event: dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:  # observability must never break the call itself
            logger.exception("LLM event sink failed")
