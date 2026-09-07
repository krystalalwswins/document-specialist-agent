"""Tool-layer retry policy + error classification.

The policy is a pure decision maker: given the attempt number and an
ErrorType, it says whether to retry, how long to wait, and why. The Executor
owns the actual loop (sleep + re-invoke), keeping the policy unit-testable
without any IO.
"""

from __future__ import annotations

import errno
import math
import random
from dataclasses import dataclass
from typing import Optional

from tools.base_tool import ErrorType


def classify_exception(exc: Exception, _seen: frozenset[int] = frozenset()) -> ErrorType:
    """Classify specific errors before broad bases; preserve explicit wrapped causes."""
    if id(exc) in _seen:
        return ErrorType.BUSINESS
    _seen = _seen | {id(exc)}
    if isinstance(exc, PermissionError):
        return ErrorType.PERMISSION_DENIED
    if isinstance(exc, (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError)):
        return ErrorType.INVALID_ARGUMENT
    if isinstance(exc, TimeoutError):
        return ErrorType.TIMEOUT
    if isinstance(exc, ConnectionError):
        return ErrorType.TRANSIENT
    if isinstance(exc, OSError):
        if exc.errno in {errno.EAGAIN, errno.ECONNRESET, errno.ECONNREFUSED, errno.ENETUNREACH}:
            return ErrorType.TRANSIENT
        return ErrorType.BUSINESS  # e.g. disk full is not a network failure

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
            if status in (408, 429) or (isinstance(status, int) and 500 <= status <= 599):
                return ErrorType.TRANSIENT
            return ErrorType.INVALID_ARGUMENT

    # agent-sandbox ApiError and boto3 ClientError expose different status shapes.
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    code = None
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", status)
        code = response.get("Error", {}).get("Code")
    if status in (401, 403) or code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
        return ErrorType.PERMISSION_DENIED
    transient_codes = {"SlowDown", "Throttling", "RequestTimeout", "ServiceUnavailable"}
    if status in (408, 429) or (isinstance(status, int) and 500 <= status <= 599) or code in transient_codes:
        return ErrorType.TRANSIENT
    if (isinstance(status, int) and 400 <= status <= 499) or code in {"NoSuchKey", "NoSuchBucket"}:
        return ErrorType.INVALID_ARGUMENT
    if isinstance(exc.__cause__, Exception):
        return classify_exception(exc.__cause__, _seen)
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
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not all(math.isfinite(x) for x in (base_delay, backoff_factor, max_delay)):
            raise ValueError("backoff parameters must be finite")
        if base_delay < 0 or max_delay < 0 or backoff_factor < 1:
            raise ValueError("delays must be non-negative and backoff_factor >= 1")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay
        self.jitter = jitter

    def decide(self, attempt: int, error_type: Optional[ErrorType], *, retry_safe: bool = True) -> RetryDecision:
        if error_type is None or not error_type.retryable:
            label = error_type.value if error_type else "unknown"
            return RetryDecision(False, 0.0, f"non_retryable_{label}", True)

        if not retry_safe:
            return RetryDecision(False, 0.0, "unsafe_to_replay", True)

        if attempt >= self.max_attempts:
            return RetryDecision(False, 0.0, "max_attempts_exhausted", True)

        delay = self._backoff(attempt)
        return RetryDecision(True, delay, f"retryable_{error_type.value}", False)

    def _backoff(self, attempt: int) -> float:
        raw = self.base_delay * (self.backoff_factor ** (attempt - 1))
        if self.jitter:
            raw = raw * random.uniform(0.5, 1.5)
        return round(min(raw, self.max_delay), 3)
