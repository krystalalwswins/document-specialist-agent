"""Tool-layer retry policy + error classification.

The policy is a pure decision maker: given the attempt number and an
ErrorType, it says whether to retry, how long to wait, and why. The Executor
owns the actual loop (sleep + re-invoke), keeping the policy unit-testable
without any IO.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from tools.base_tool import ErrorType


def classify_exception(exc: Exception) -> ErrorType:
    """Fallback classifier for tools that raise instead of returning ToolResult."""
    if isinstance(exc, TimeoutError):
        return ErrorType.TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorType.TRANSIENT

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is installed
        httpx = None
    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return ErrorType.TIMEOUT
        if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
            return ErrorType.TRANSIENT
        if isinstance(exc, httpx.HTTPStatusError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                return ErrorType.PERMISSION_DENIED
            if status == 429 or (status is not None and status >= 500):
                return ErrorType.TRANSIENT
            return ErrorType.INVALID_ARGUMENT

    return ErrorType.BUSINESS


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    delay_seconds: float
    reason: str
    final: bool


class RetryPolicy:
    """Decides whether a failed tool call should be retried.

    Backoff is exponential: base_delay * backoff_factor ** (attempt - 1),
    capped at max_delay. Jitter randomizes each delay so many concurrent
    failures don't all retry at the exact same instant (thundering herd).
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 10.0,
        jitter: bool = True,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay
        self.jitter = jitter

    def decide(self, attempt: int, error_type: Optional[ErrorType]) -> RetryDecision:
        if error_type is None or not error_type.retryable:
            label = error_type.value if error_type else "unknown"
            return RetryDecision(False, 0.0, f"non_retryable_{label}", True)

        if attempt >= self.max_attempts:
            return RetryDecision(False, 0.0, "max_attempts_exhausted", True)

        delay = self._backoff(attempt)
        return RetryDecision(True, delay, f"retryable_{error_type.value}", False)

    def _backoff(self, attempt: int) -> float:
        raw = self.base_delay * (self.backoff_factor ** (attempt - 1))
        if self.jitter:
            raw = raw * random.uniform(0.5, 1.5)
        return round(min(raw, self.max_delay), 3)
