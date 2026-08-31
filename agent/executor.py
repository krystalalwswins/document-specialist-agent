"""Executor: the multi-round tool-calling loop (the Agent core, not a script)."""

from __future__ import annotations

import json
from typing import Any

from agent.llm_client import LLMClient
from agent.planner import Plan
from task.task_manager import TaskManager
from tools.tool_registry import ToolRegistry


class MaxIterationsError(Exception):
    """Raised when the tool-calling loop exceeds max_iterations."""


class Executor:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        task_manager: TaskManager,
        max_iterations: int = 8,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._task_manager = task_manager
        self._max_iterations = max_iterations

    def run(self, task_id: str, user_input: str, plan: Plan) -> str:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a task-execution agent. Execute the plan step by step "
                    "using the available tools, then produce a final answer."
                ),
            },
            {"role": "user", "content": f"Task: {user_input}\n\nPlan:\n{plan.summary()}"},
        ]

        for _ in range(self._max_iterations):
            response = self._llm.chat(messages, tools=self._registry.to_openai_tools())
            message = response.choices[0].message
            messages.append(self._assistant_message(message))

            if not message.tool_calls:
                return message.content or ""

            for call in message.tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                step = self._task_manager.add_step(task_id, call.function.name, tool=call.function.name)
                self._task_manager.start_step(task_id, step.id)
                try:
                    result = self._registry.execute(call.function.name, arguments)
                except Exception as exc:
                    self._task_manager.fail_step(task_id, step.id, str(exc))
                    content = f"[tool error] {exc}"
                else:
                    if result.success:
                        self._task_manager.succeed_step(task_id, step.id, output=result.output)
                    else:
                        self._task_manager.fail_step(
                            task_id, step.id, error=result.error or "unknown error"
                        )
                    content = result.to_text()

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )

        raise MaxIterationsError(f"exceeded {self._max_iterations} tool-calling iterations")

    @staticmethod
    def _assistant_message(message: Any) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        return msg
