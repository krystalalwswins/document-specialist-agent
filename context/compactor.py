"""LLM-backed structured compaction for complete conversation groups."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator

from agent.llm_client import LLMClient
from context.message_groups import MessageGroup, flatten_groups


RESULT_REF_PATTERN = re.compile(r"\bout_[0-9a-f]{32}\b")
SUMMARY_MARKER = "[CONTEXT SUMMARY]\n"

SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "compact_context",
        "description": "Summarize older Agent history without inventing facts.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string", "minLength": 1},
                "active_plan": {"type": "string", "minLength": 1},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "confirmed_facts": {"type": "array", "items": {"type": "string"}},
                "result_references": {"type": "array", "items": {"type": "string"}},
                "completed_work": {"type": "array", "items": {"type": "string"}},
                "unfinished_work": {"type": "array", "items": {"type": "string"}},
                "important_errors": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "goal",
                "active_plan",
                "constraints",
                "confirmed_facts",
                "result_references",
                "completed_work",
                "unfinished_work",
                "important_errors",
            ],
        },
    },
}


class ContextCompactionError(RuntimeError):
    def __init__(self, message: str, *, attempts: int, omitted_groups: int) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.omitted_groups = omitted_groups


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


@dataclass
class ContextSummary:
    goal: str
    active_plan: str
    constraints: list[str] = field(default_factory=list)
    confirmed_facts: list[str] = field(default_factory=list)
    result_references: list[str] = field(default_factory=list)
    completed_work: list[str] = field(default_factory=list)
    unfinished_work: list[str] = field(default_factory=list)
    important_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "active_plan": self.active_plan,
            "constraints": _unique(self.constraints),
            "confirmed_facts": _unique(self.confirmed_facts),
            "result_references": _unique(self.result_references),
            "completed_work": _unique(self.completed_work),
            "unfinished_work": _unique(self.unfinished_work),
            "important_errors": _unique(self.important_errors),
        }

    def to_message(self, max_chars: int) -> dict[str, str]:
        payload = self.to_dict()
        # Keep the schema valid while bounding model-generated verbosity. Result
        # references, goal, active plan and unfinished work are removed last.
        removable = ["confirmed_facts", "important_errors", "completed_work", "constraints"]
        while len(json.dumps(payload, ensure_ascii=False)) > max_chars:
            changed = False
            for key in removable:
                if payload[key]:
                    payload[key].pop(0)
                    changed = True
                    break
            if changed:
                continue
            for key in ("goal", "active_plan"):
                if len(payload[key]) > 500:
                    payload[key] = payload[key][:500] + "..."
                    changed = True
                    break
            if not changed:
                break
        return {
            "role": "system",
            "content": SUMMARY_MARKER + json.dumps(payload, ensure_ascii=False),
        }

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> "ContextSummary":
        return cls(**arguments)


@dataclass(frozen=True)
class CompactionResult:
    summary: ContextSummary
    attempts: int
    omitted_groups: int


class ContextCompactor:
    def __init__(self, llm: LLMClient, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._llm = llm
        self._max_attempts = max_attempts
        self._validator = Draft202012Validator(SUMMARY_TOOL["function"]["parameters"])

    def compact(
        self,
        groups: list[MessageGroup],
        *,
        goal: str,
        active_plan: str,
        completed_work: list[str],
        unfinished_work: list[str],
        existing_summary: ContextSummary | None = None,
        on_llm_event: Callable[[dict[str, Any]], None] | None = None,
        on_retry: Callable[[dict[str, Any]], None] | None = None,
    ) -> CompactionResult:
        working = list(groups)
        omitted: list[MessageGroup] = []
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._llm.chat(
                    self._messages(
                        working,
                        goal=goal,
                        active_plan=active_plan,
                        completed_work=completed_work,
                        unfinished_work=unfinished_work,
                        existing_summary=existing_summary,
                    ),
                    tools=[SUMMARY_TOOL],
                    tool_choice={
                        "type": "function",
                        "function": {"name": "compact_context"},
                    },
                    on_event=on_llm_event,
                )
                summary = self._parse(response)
                # Runtime-owned anchors are never delegated to the summarizer.
                summary.goal = goal
                summary.active_plan = active_plan
                summary.completed_work = _unique(completed_work + summary.completed_work)
                summary.unfinished_work = _unique(unfinished_work + summary.unfinished_work)
                summary.result_references = _unique(
                    summary.result_references
                    + self.extract_result_refs(groups)
                    + (existing_summary.result_references if existing_summary else [])
                )
                if omitted:
                    summary.constraints.append(
                        f"{len(omitted)} earliest complete message group(s) were omitted "
                        "from the compaction request after bounded retry."
                    )
                    summary.confirmed_facts.extend(self._bounded_facts(omitted))
                return CompactionResult(summary, attempt, len(omitted))
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_attempts or not working:
                    break
                omitted.append(working.pop(0))
                if on_retry is not None:
                    on_retry(
                        {
                            "attempt": attempt,
                            "reason": type(exc).__name__,
                            "omitted_groups": len(omitted),
                            "remaining_groups": len(working),
                        }
                    )
        raise ContextCompactionError(
            f"context compaction failed: {type(last_error).__name__ if last_error else 'unknown'}",
            attempts=self._max_attempts,
            omitted_groups=len(omitted),
        ) from last_error

    @staticmethod
    def extract_result_refs(groups: list[MessageGroup]) -> list[str]:
        text = json.dumps(flatten_groups(groups), ensure_ascii=False, default=str)
        return _unique(RESULT_REF_PATTERN.findall(text))

    @staticmethod
    def _bounded_facts(groups: list[MessageGroup]) -> list[str]:
        facts: list[str] = []
        for message in flatten_groups(groups):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                facts.append(content.strip()[:300])
            if len(facts) >= 6:
                break
        return facts

    @staticmethod
    def _messages(
        groups: list[MessageGroup],
        *,
        goal: str,
        active_plan: str,
        completed_work: list[str],
        unfinished_work: list[str],
        existing_summary: ContextSummary | None,
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Compact older Agent history into the required schema. Preserve only "
                    "confirmed facts. Never claim a tool succeeded unless its result says so. "
                    "Keep every result_ref, constraint, unfinished item and important error."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": goal,
                        "active_plan": active_plan,
                        "completed_work": completed_work,
                        "unfinished_work": unfinished_work,
                        "existing_summary": (
                            existing_summary.to_dict() if existing_summary else None
                        ),
                        "older_complete_message_groups": [
                            group.to_list() for group in groups
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]

    def _parse(self, response: Any) -> ContextSummary:
        calls = response.choices[0].message.tool_calls
        if len(calls or []) != 1 or calls[0].function.name != "compact_context":
            raise ValueError("expected exactly one compact_context call")
        arguments = json.loads(calls[0].function.arguments)
        self._validator.validate(arguments)
        return ContextSummary.from_arguments(arguments)
