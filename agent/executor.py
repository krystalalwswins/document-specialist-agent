"""Executor: the multi-round tool-calling loop (the Agent core, not a script)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from agent.llm_client import LLMClient
from agent.planner import Plan
from retry.retry_policy import RetryDecision, RetryPolicy, classify_exception
from task.task_manager import TaskManager
from tools.base_tool import ErrorType, ToolResult
from tools.tool_registry import ToolRegistry


class MaxIterationsError(Exception):
    """Raised when the tool-calling loop exceeds max_iterations."""


class UnsafeExecutionStateError(Exception):
    """Do not let the model replay code after timeout or uncertain cleanup."""


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
            {"role": "user", "content": f"Task: {user_input}\n\nPlan:\n{plan.summary()}{self._input_context(task_id)}"},
        ]

        for iteration in range(1, self._max_iterations + 1):
            response = self._llm.chat(
                messages,
                tools=self._registry.to_openai_tools(),
                on_event=self._task_manager.metric_sink(
                    task_id, "llm_events", phase="execute", iteration=iteration
                ),
            )
            message = response.choices[0].message
            messages.append(self._assistant_message(message))

            if not message.tool_calls:
                return message.content or ""

            for call in message.tool_calls:
                step = self._task_manager.add_step(task_id, call.function.name, tool=call.function.name)
                self._task_manager.start_step(task_id, step.id)
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except (ValueError, TypeError):
                    arguments = None  # Registry produces INVALID_ARGUMENT without invoking a tool.

                result, _, attempts = self._invoke_tool(
                    task_id, call.function.name, arguments, step_id=step.id, tool_call_id=call.id,
                )
                self._task_manager.set_step_attempts(task_id, step.id, attempts)

                if result.success:
                    self._task_manager.succeed_step(task_id, step.id, output=result.output)
                    if result.metadata:
                        self._task_manager.add_artifact(task_id, dict(result.metadata))
                else:
                    self._task_manager.fail_step(
                        task_id, step.id, error=result.error or "unknown error"
                    )
                if result.terminal:
                    raise UnsafeExecutionStateError(result.error or "unsafe sandbox execution state")
                content = result.to_text()

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )

        raise MaxIterationsError(f"exceeded {self._max_iterations} tool-calling iterations")

    def _input_context(self, task_id: str) -> str:
        """Tell the model which sandbox directory and input files belong to this task."""
        task = self._task_manager.get_task(task_id)
        if not task.workspace_dir:
            return ""
        parts = [
            f"\n\nThis task owns the sandbox directory: {task.workspace_dir}",
            "Relative paths in your code, and file arguments to tools, resolve inside it.",
        ]
        staged = [item for item in task.input_files if item.get("sandbox_path")]
        if staged:
            lines = "\n".join(
                f"- {item['sandbox_path']} ({item.get('bytes', 0)} bytes)" for item in staged
            )
            parts.append(f"Input files already staged there:\n{lines}")
        parts.append(f"Write every output inside {task.workspace_dir} as well.")
        return "\n".join(parts)

    def _invoke_tool(
        self, task_id: str, name: str, arguments: Any,
        *, step_id: str | None = None, tool_call_id: str | None = None,
    ) -> tuple[ToolResult, list[dict[str, Any]], int]:
        events: list[dict[str, Any]] = []
        attempt = 0
        result: ToolResult

        def record(event: dict[str, Any]) -> None:
            event.update(step_id=step_id, tool_call_id=tool_call_id)
            events.append(event)
            # Write after each attempt, before backoff; batch 2 replaces the in-memory store.
            self._record_retry_events(task_id, [event])
            if step_id is not None:
                self._task_manager.set_step_attempts(task_id, step_id, attempt)
            if event["error_type"] == ErrorType.PERMISSION_DENIED.value:
                self._task_manager.add_metric_events(task_id, "security_events", [{
                    "task_id": task_id, "step_id": step_id, "tool_call_id": tool_call_id,
                    "tool_name": name, "decision": "DENIED", "reason": "permission_denied",
                    "occurred_at": event["occurred_at"],
                }])

        while True:
            attempt += 1
            started = time.monotonic()
            try:
                result = self._registry.execute(name, arguments, task_id=task_id)
                error_type = None if result.success else result.error_type
                error_message = result.error if not result.success else None
            except Exception as exc:
                error_type = classify_exception(exc)
                error_message = str(exc)
                result = ToolResult(success=False, error=error_message, error_type=error_type, terminal=getattr(exc, "terminal", False))

            duration_ms = int((time.monotonic() - started) * 1000)

            if result.success:
                record(
                    self._make_event(task_id, name, attempt, None, None, "success", duration_ms, "SUCCESS")
                )
                return result, events, attempt

            decision = (
                RetryDecision(False, 0, "execution_state_unknown", True) if result.terminal
                else self._retry_policy.decide(attempt, error_type, retry_safe=self._registry.retry_safe(name))
            )
            record(
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
            "occurred_at": datetime.now(timezone.utc).isoformat(),
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
