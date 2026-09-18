"""Planner: turn a user request into a structured execution plan via the LLM."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from jsonschema import Draft202012Validator, ValidationError

from agent.llm_client import LLMClient
from task.plan_model import Plan, PlanStep, assert_replan_preserves_completed_steps


class PlannerError(Exception):
    """Raised when the planner cannot produce a valid plan."""


CREATE_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "create_plan",
        "description": "Create an ordered execution plan. Each step optionally names a tool.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "step_id": {"type": "string", "minLength": 1},
                            "name": {"type": "string", "minLength": 1},
                            "description": {"type": "string", "minLength": 1},
                            "tool": {"type": ["string", "null"], "minLength": 1},
                            "depends_on": {"type": "array", "uniqueItems": True,
                                           "items": {"type": "string", "minLength": 1}},
                            "completion_criteria": {"type": "array", "minItems": 1,
                                                    "items": {"type": "string", "minLength": 1}},
                        },
                        "required": ["step_id", "name", "description", "depends_on", "completion_criteria"],
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
                    " Give each step a unique step_id, depends_on (IDs of prerequisites, "
                    "empty for independent steps), and non-empty completion_criteria "
                    "describing observable evidence of completion. Dependencies must be acyclic."
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
        try:
            return self._parse_plan_response(response, user_input, 1)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError) as exc:
            raise PlannerError(f"invalid plan: {exc}") from exc

    def replan(
        self,
        user_input: str,
        plan: Plan,
        *,
        completed_steps: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        available_tools: list[str],
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> Plan:
        """Produce the next plan version when reality no longer matches the plan.

        Inputs are exactly what the runtime knows: the original goal, the current
        plan, the steps already judged complete (with their evidence), the failure
        observations, and the tools the model is actually allowed to call. Only the
        unfinished part may change; completed steps must come back unchanged.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a planning agent repairing a running plan. Produce a new "
                    "complete plan with create_plan. Keep every completed step exactly as "
                    "given (same step_id, name, description, tool, depends_on and "
                    "completion_criteria) and replace only the unfinished part. Never "
                    "re-add work that is already complete. New steps need unique ids, "
                    "acyclic depends_on and non-empty completion_criteria, and may only "
                    "use the available tools."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original goal:\n{user_input}\n\n"
                    f"Current plan v{plan.version}:\n{json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)}\n\n"
                    "Completed steps (must stay unchanged):\n"
                    f"{json.dumps(completed_steps, ensure_ascii=False, indent=2)}\n\n"
                    "Observations that triggered this replan:\n"
                    f"{json.dumps(observations, ensure_ascii=False, indent=2)}\n\n"
                    f"Available tools: {', '.join(available_tools) or '(none)'}"
                ),
            },
        ]
        response = self._llm.chat(
            messages,
            tools=[CREATE_PLAN_TOOL],
            tool_choice={"type": "function", "function": {"name": "create_plan"}},
            on_event=on_event,
        )
        try:
            new_plan = self._parse_plan_response(response, user_input, plan.version + 1)
            assert_replan_preserves_completed_steps(
                plan, new_plan, [item["step_id"] for item in completed_steps]
            )
            return new_plan
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError) as exc:
            raise PlannerError(f"invalid plan: {exc}") from exc

    @staticmethod
    def _parse_plan_response(response: Any, user_input: str, version: int) -> Plan:
        """Shared validation path for the first plan and every replanned version."""
        calls = response.choices[0].message.tool_calls
        if not calls:
            raise PlannerError("LLM did not return a plan")
        if len(calls) != 1 or calls[0].function.name != "create_plan":
            raise PlannerError("expected exactly one create_plan call")
        args = json.loads(calls[0].function.arguments)
        if isinstance(args, dict) and args.get("steps") == []:
            raise PlannerError("plan is empty")
        Draft202012Validator(CREATE_PLAN_TOOL["function"]["parameters"]).validate(args)
        # Identity and version belong to the runtime, not the model.
        return Plan.from_dict({"user_input": user_input, "version": version, "steps": args["steps"]})
