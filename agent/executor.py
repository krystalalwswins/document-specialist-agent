"""Executor: the multi-round tool-calling loop (the Agent core, not a script)."""

from __future__ import annotations

import json
import time
from typing import Any

from agent.llm_client import LLMClient
from agent.planner import Plan
from retry.retry_policy import RetryPolicy, classify_exception
from task.task_manager import TaskManager
from tools.base_tool import ToolResult
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
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._task_manager = task_manager
        self._max_iterations = max_iterations
        self._retry_policy = retry_policy or RetryPolicy()

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

                result, events, attempts = self._invoke_tool(
                    task_id, call.function.name, arguments
                )
                self._task_manager.set_step_attempts(task_id, step.id, attempts)
                self._record_retry_events(task_id, events)

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

    def _invoke_tool(
        self, task_id: str, name: str, arguments: dict[str, Any]
    ) -> tuple[ToolResult, list[dict[str, Any]], int]:
        events: list[dict[str, Any]] = []
        attempt = 0
        result: ToolResult

        while True:
            attempt += 1
            started = time.monotonic()
            try:
                result = self._registry.execute(name, arguments)
                error_type = None if result.success else result.error_type
                error_message = result.error if not result.success else None
            except Exception as exc:
                error_type = classify_exception(exc)
                error_message = str(exc)
                result = ToolResult(success=False, error=error_message, error_type=error_type)

            duration_ms = int((time.monotonic() - started) * 1000)

            if result.success:
                events.append(
                    self._make_event(task_id, name, attempt, None, None, "success", duration_ms, "SUCCESS")
                )
                return result, events, attempt

            decision = self._retry_policy.decide(attempt, error_type)
            events.append(
                self._make_event(
                    task_id,
                    name,
                    attempt,
                    error_type,
                    error_message,
                    decision.reason,
                    duration_ms,
                    "FAILED" if decision.final else "RETRYING",
                )
            )

            if decision.should_retry:
                time.sleep(decision.delay_seconds)
                continue
            return result, events, attempt

    def _record_retry_events(self, task_id: str, events: list[dict[str, Any]]) -> None:
        if events:
            self._task_manager.add_metric_events(task_id, "retry_events", events)

    @staticmethod
    def _make_event(
        task_id: str,
        tool_name: str,
        attempt: int,
        error_type: Any,
        error_message: Any,
        retry_reason: str,
        duration_ms: int,
        final_status: str,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "tool_name": tool_name,
            "attempt": attempt,
            "error_type": error_type.value if error_type else None,
            "error_message": error_message,
            "retry_reason": retry_reason,
            "duration_ms": duration_ms,
            "final_status": final_status,
        }

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
