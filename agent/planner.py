"""Planner: turn a user request into a structured execution plan via the LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.llm_client import LLMClient


class PlannerError(Exception):
    """Raised when the planner cannot produce a valid plan."""


@dataclass
class PlanStep:
    name: str
    description: str = ""
    tool: Optional[str] = None


@dataclass
class Plan:
    user_input: str
    steps: list[PlanStep] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{i + 1}. {step.name}" + (f" (tool: {step.tool})" if step.tool else "")
            for i, step in enumerate(self.steps)
        ]
        return "\n".join(lines) if lines else "(no steps)"


CREATE_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "create_plan",
        "description": "Create an ordered execution plan. Each step optionally names a tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "tool": {"type": "string"},
                        },
                        "required": ["name", "description"],
                    },
                },
            },
            "required": ["steps"],
        },
    },
}


class Planner:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def plan(
        self,
        user_input: str,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> Plan:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a planning agent. Decompose the user task into a small, "
                    "ordered list of concrete steps. Name a tool only when a step needs one."
                ),
            },
            {"role": "user", "content": user_input},
        ]
        response = self._llm.chat(
            messages,
            tools=[CREATE_PLAN_TOOL],
            tool_choice={"type": "function", "function": {"name": "create_plan"}},
            on_event=on_event,
        )
        message = response.choices[0].message
        if not message.tool_calls:
            raise PlannerError("LLM did not return a plan")

        args = json.loads(message.tool_calls[0].function.arguments or "{}")
        steps = [
            PlanStep(
                name=step.get("name", ""),
                description=step.get("description", ""),
                tool=step.get("tool"),
            )
            for step in args.get("steps", [])
        ]
        if not steps:
            raise PlannerError("plan is empty")
        return Plan(user_input=user_input, steps=steps)
