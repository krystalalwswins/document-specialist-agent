"""Executor: the multi-round tool-calling loop (the Agent core, not a script)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm_client import LLMClient
    from tools.tool_registry import ToolRegistry
from agent.planner import Plan
from retry.retry_policy import RetryDecision, RetryPolicy, classify_exception
from task.task_manager import TaskManager
from tools.base_tool import ErrorType, ToolResult
from agent.runtime import current_run, check_budget
from memory.context import compact_messages, result_reference


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
        context_max_chars: int = 24000,
    ) -> None:
        self._context_max_chars = context_max_chars
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

        corrections = 0
        for _ in range(self._max_iterations):
            check_budget()
            messages = compact_messages(messages, self._context_max_chars)
            self._task_manager.add_metric_events(task_id, 'llm_request_events', [{'messages': messages}])
            response = self._llm.chat(messages, tools=self._registry.to_openai_tools())
            check_budget()
            message = response.choices[0].message
            assistant = self._assistant_message(message)
            self._task_manager.add_metric_events(task_id, 'message_events', [assistant])
            messages.append(assistant)

            if not message.tool_calls:
                context = current_run.get()
                missing = (context and context.requirements.get('required') and
                           not self._task_manager.get_task(task_id).metrics.get('artifacts'))
                if missing and corrections < 2:
                    corrections += 1
                    messages.append({'role': 'user', 'content':
                                     'No validated artifact has been saved. Correct the file and call save_report before finishing.'})
                    continue
                if missing:
                    raise ValueError('required_artifact_missing')
                return message.content or ""

            for call in message.tool_calls:
                check_budget()
                step = self._task_manager.add_step(task_id, call.function.name, tool=call.function.name)
                self._task_manager.start_step(task_id, step.id)
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except (ValueError, TypeError):
                    arguments = None  # Registry produces INVALID_ARGUMENT without invoking a tool.

                self._task_manager.add_metric_events(task_id, 'tool_call_events', [{
                    'step_id': step.id, 'tool_call_id': call.id,
                    'tool_name': call.function.name, 'status': 'STARTED',
                }])
                result, _, attempts = self._invoke_tool(
                    task_id, call.function.name, arguments, step_id=step.id, tool_call_id=call.id,
                )
                self._task_manager.set_step_attempts(task_id, step.id, attempts)
                self._task_manager.add_metric_events(task_id, 'tool_call_events', [{
                    'step_id': step.id, 'tool_call_id': call.id,
                    'tool_name': call.function.name, 'status': 'SUCCESS' if result.success else 'FAILED',
                }])

                if result.success:
                    if result.artifact:
                        self._task_manager.add_metric_events(task_id, 'artifacts', [result.artifact])
                    self._task_manager.succeed_step(task_id, step.id, output=result.output)
                else:
                    self._task_manager.fail_step(
                        task_id, step.id, error=result.error or "unknown error"
                    )
                if result.terminal:
                    raise UnsafeExecutionStateError(result.error or "unsafe sandbox execution state")
                content = result_reference(result.to_text(), task_id, step.id)

                tool_message = {"role": "tool", "tool_call_id": call.id, "content": content}
                self._task_manager.add_metric_events(task_id, 'message_events', [tool_message])
                messages.append(tool_message)

        raise MaxIterationsError(f"exceeded {self._max_iterations} tool-calling iterations")

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
            context = current_run.get()
            if context:
                context.consume_tool()
            attempt += 1
            started = time.monotonic()
            try:
                result = self._registry.execute(name, arguments)
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
                check_budget()
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
                if context and time.monotonic() + decision.delay_seconds >= context.deadline:
                    from agent.runtime import BudgetExceeded
                    raise BudgetExceeded('task_deadline_exceeded_before_retry')
                time.sleep(decision.delay_seconds)
                continue
            check_budget()
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
