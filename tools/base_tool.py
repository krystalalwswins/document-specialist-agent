"""Base tool abstraction + result type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


class ToolError(Exception):
    """Raised when a tool cannot be found or fails."""


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    error: Optional[str] = None

    def to_text(self) -> str:
        """Human-readable text to feed back to the LLM."""
        if self.success:
            return self.output
        return f"[tool error] {self.error or 'unknown error'}"


class BaseTool(ABC):
    """A tool the agent can call. Concrete tools live in tools/ (Phase 1)."""

    name: str
    description: str

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
