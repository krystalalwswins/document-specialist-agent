"""Base tool abstraction + result type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorType(str, Enum):
    """Classifies why a tool call failed, and whether it is retryable.

    TRANSIENT         temporary infrastructure fault (network, 5xx, 429, cold start) -> retry
    TIMEOUT           the operation timed out                                            -> retry (bounded)
    INVALID_ARGUMENT  bad call arguments                                                 -> no retry (same args fail again)
    PERMISSION_DENIED authorization failure                                              -> no retry (retry won't grant access)
    EXECUTION         code ran but raised (e.g. NameError)                                -> no retry
    BUSINESS          logic-level failure (no result, wrong file)                         -> no retry
    """

    TRANSIENT = "TRANSIENT"
    TIMEOUT = "TIMEOUT"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    EXECUTION = "EXECUTION"
    BUSINESS = "BUSINESS"

    @property
    def retryable(self) -> bool:
        return self in (ErrorType.TRANSIENT, ErrorType.TIMEOUT)


class ToolError(Exception):
    """Raised when a tool cannot be found or fails."""


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    error: Optional[str] = None
    error_type: Optional[ErrorType] = None
    terminal: bool = False  # Unsafe/unknown execution state: stop the task, not just this step.
    # Machine-readable payload for the runtime (e.g. an artifact record). The LLM
    # only ever sees ``to_text()``; this is for lifecycle/validation bookkeeping.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        """Human-readable text to feed back to the LLM."""
        if self.success:
            return self.output
        return f"[tool error] {self.error or 'unknown error'}"


class BaseTool(ABC):
    """A tool the agent can call. Concrete tools live in tools/ (Phase 1)."""

    name: str
    description: str
    retry_safe: bool = False  # Explicit opt-in; unknown side effects must not be replayed.
    required_permissions: frozenset[str] = frozenset()
    file_parameters: tuple[str, ...] = ()
    object_key_parameters: tuple[str, ...] = ()
    # Tools that run user code inside the sandbox get the per-task directory as
    # their working directory, injected by the registry (never by the model).
    task_scoped_cwd: bool = False

    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """Return the JSON Schema for this tool's arguments."""

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Run the tool and return a ToolResult."""

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            },
        }
