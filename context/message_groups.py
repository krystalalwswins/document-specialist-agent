"""Group chat history without splitting tool calls from their tool results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ContextIntegrityError(RuntimeError):
    """The history contains an incomplete or mismatched tool-call group."""


@dataclass(frozen=True)
class MessageGroup:
    messages: tuple[dict[str, Any], ...]

    def to_list(self) -> list[dict[str, Any]]:
        return [dict(message) for message in self.messages]


def group_messages(messages: list[dict[str, Any]]) -> list[MessageGroup]:
    """Return atomic history groups and reject broken tool-call pairings."""
    groups: list[MessageGroup] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            if message.get("role") == "tool":
                raise ContextIntegrityError("orphan_tool_result")
            groups.append(MessageGroup((dict(message),)))
            index += 1
            continue

        expected = [call.get("id") for call in calls if isinstance(call, dict)]
        if not expected or any(not isinstance(call_id, str) for call_id in expected):
            raise ContextIntegrityError("assistant_tool_call_missing_id")
        collected: list[dict[str, Any]] = [dict(message)]
        actual: list[str] = []
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            result = messages[cursor]
            call_id = result.get("tool_call_id")
            if not isinstance(call_id, str):
                raise ContextIntegrityError("tool_result_missing_call_id")
            actual.append(call_id)
            collected.append(dict(result))
            cursor += 1
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise ContextIntegrityError("tool_call_result_mismatch")
        groups.append(MessageGroup(tuple(collected)))
        index = cursor
    return groups


def flatten_groups(groups: list[MessageGroup]) -> list[dict[str, Any]]:
    return [message for group in groups for message in group.to_list()]
