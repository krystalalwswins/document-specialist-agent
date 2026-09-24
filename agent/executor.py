"""Executor: the multi-round tool-calling loop (the Agent core, not a script).

P0-2 adds the plan runtime around the existing loop: every tool call is attributed
to a plan step, a step only runs after its dependencies are complete, completion is
judged from explicit evidence instead of "the tool returned success", and the model
can ask for a budgeted, versioned local replan.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone
from typing import Any

from agent.llm_client import LLMClient
from agent.planner import Plan, Planner, PlannerError
from context.hooks import AfterToolCallHook, PreparedToolOutput
from context.manager import ContextManager, ContextSnapshot
from retry.retry_policy import RetryDecision, RetryPolicy, classify_exception
from task.plan_model import PlanValidationError
from task.plan_state import PlanRunState
from task.task_manager import TaskManager
from task.task_model import TaskStateError
from tools.base_tool import ErrorType, ToolResult
from tools.tool_registry import ToolRegistry

# Reserved argument added to every registered tool schema and stripped again before
# the tool schema is validated, so the model always says which planned step it works on.
PLAN_STEP_ARGUMENT = "plan_step_id"

# Runtime control calls handled inside the loop. They are not registered tools:
# they grant no capability, so the registry, permissions and audit stay untouched.
COMPLETE_PLAN_STEP = "complete_plan_step"
REQUEST_REPLAN = "request_replan"
REPLAN_REASON_CODES = ("tool_failure", "missing_data", "criteria_unmet", "dependency_invalid")
MAX_RETRY_EVENT_ERROR_CHARS = 2000

COMPLETE_PLAN_STEP_TOOL = {
    "type": "function",
    "function": {
        "name": COMPLETE_PLAN_STEP,
        "description": (
            "Record that a planned step is finished. Provide the evidence that satisfies "
            "that step's completion_criteria. A tool call returning success does not "
            "complete a step by itself, and a completed step cannot be completed again."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "step_id": {"type": "string", "minLength": 1},
                "evidence": {
                    "type": "string",
                    "minLength": 1,
                    "description": "observable result that satisfies the completion criteria",
                },
            },
            "required": ["step_id", "evidence"],
        },
    },
}

REQUEST_REPLAN_TOOL = {
    "type": "function",
    "function": {
        "name": REQUEST_REPLAN,
        "description": (
            "Ask the runtime to replace the unfinished part of the plan. Only allowed when "
            "a tool failed unrecoverably, required data is missing, completion criteria "
            "cannot be met, or the plan's dependencies no longer hold. Completed steps are "
            "kept unchanged and can never be replanned or replayed."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reason_code": {"type": "string", "enum": list(REPLAN_REASON_CODES)},
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "the observation that makes the current plan inadequate",
                },
            },
            "required": ["reason_code", "reason"],
        },
    },
}

class MaxIterationsError(Exception):
    """Raised when the tool-calling loop exceeds max_iterations."""


class UnsafeExecutionStateError(Exception):
    """Do not let the model replay code after timeout or uncertain cleanup."""


class ReplanBudgetExceededError(Exception):
    """Raised when a task asks for more local replans than its budget allows."""


class ToolOutputProcessingError(Exception):
    """The runtime could not safely persist or bound a tool observation."""


class Executor:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        task_manager: TaskManager,
        max_iterations: int = 8,
        retry_policy: RetryPolicy | None = None,
        planner: Planner | None = None,
        max_replans: int = 2,
        after_tool_call: AfterToolCallHook | None = None,
        context_manager: ContextManager | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._task_manager = task_manager
        self._max_iterations = max_iterations
        self._retry_policy = retry_policy or RetryPolicy()
        # None means "no local replanning configured": a request becomes an observation.
        self._planner = planner
        self._max_replans = max_replans
        self._after_tool_call = after_tool_call
        self._context_manager = context_manager

    def run(
        self,
        task_id: str,
        user_input: str,
        plan: Plan,
        repair_hint: str | None = None,
        memory_context: str | None = None,
    ) -> str:
        """Run the tool-calling loop until the model stops calling tools.

        ``repair_hint`` is only set by the orchestrator's recovery path: it tells
        the model why the previous attempt was rejected so it can fix the
        deliverable instead of repeating the same output.
        """
        state = self._plan_state(task_id, plan)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(state)},
            {
                "role": "user",
                "content": (
                    f"Task: {user_input}\n\nPlan:\n{state.plan.summary()}"
                    f"{self._input_context(task_id)}{self._memory_context(memory_context)}"
                ),
            },
        ]
        if repair_hint:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your previous attempt was rejected: {repair_hint}\n"
                        "Fix the deliverable (re-create the file and save it again) before answering."
                    ),
                }
            )

        context_session = (
            self._context_manager.new_session(pinned_count=len(messages))
            if self._context_manager is not None
            else None
        )
        replans_used = 0
        for iteration in range(1, self._max_iterations + 1):
            # Re-render instead of appending: the model always sees live progress,
            # and the message history does not grow with a status line per round.
            messages[0] = {"role": "system", "content": self._system_prompt(state)}
            tool_schemas = self._tool_schemas()
            context_sink = self._task_manager.metric_sink(
                task_id, "context_events", iteration=iteration
            )
            if self._context_manager is not None and context_session is not None:
                messages = self._context_manager.prepare(
                    messages,
                    tool_schemas,
                    context_session,
                    ContextSnapshot(
                        goal=user_input,
                        active_plan=state.plan.summary(),
                        completed_work=state.completed_ids(),
                        unfinished_work=state.unfinished_ids(),
                    ),
                    on_context_event=context_sink,
                    on_llm_event=self._task_manager.metric_sink(
                        task_id,
                        "llm_events",
                        phase="context_compaction",
                        iteration=iteration,
                    ),
                )
            response = self._llm.chat(
                messages,
                tools=tool_schemas,
                on_event=self._task_manager.metric_sink(
                    task_id, "llm_events", phase="execute", iteration=iteration
                ),
            )
            if self._context_manager is not None:
                self._context_manager.observe_response(
                    messages,
                    tool_schemas,
                    response,
                    on_context_event=context_sink,
                )
            message = response.choices[0].message
            messages.append(self._assistant_message(message))

            if not message.tool_calls:
                self._record_plan_event(task_id, {
                    "kind": "plan_finished",
                    "version": state.version,
                    "satisfied": state.all_complete(),
                    "remaining_step_ids": state.unfinished_ids(),
                })
                return message.content or ""

            for call in message.tool_calls:
                name = call.function.name
                if name == COMPLETE_PLAN_STEP:
                    state = self._complete_plan_step(task_id, state, call, messages)
                elif name == REQUEST_REPLAN:
                    state, replans_used = self._replan(
                        task_id,
                        user_input,
                        state,
                        call,
                        messages,
                        replans_used,
                        memory_context,
                    )
                else:
                    state = self._run_tool_call(task_id, state, call, messages)

        raise MaxIterationsError(f"exceeded {self._max_iterations} tool-calling iterations")

    # --- plan runtime ---------------------------------------------------------

    def _plan_state(self, task_id: str, plan: Plan) -> PlanRunState:
        """Derive step status from the task's own record, not from a caller's copy."""
        task = self._task_manager.get_task(task_id)
        return PlanRunState(task.plan or plan, task.plan_events)

    def _tool_schemas(self) -> list[dict[str, Any]]:
        schemas = [copy.deepcopy(schema) for schema in self._registry.to_openai_tools()]
        for schema in schemas:
            parameters = schema["function"].setdefault(
                "parameters", {"type": "object", "properties": {}}
            )
            parameters.setdefault("properties", {})[PLAN_STEP_ARGUMENT] = {
                "type": "string",
                "minLength": 1,
                "description": "step_id of the plan step this call belongs to",
            }
            required = parameters.setdefault("required", [])
            if PLAN_STEP_ARGUMENT not in required:
                required.append(PLAN_STEP_ARGUMENT)
        schemas.append(copy.deepcopy(COMPLETE_PLAN_STEP_TOOL))
        schemas.append(copy.deepcopy(REQUEST_REPLAN_TOOL))
        return schemas

    def _system_prompt(self, state: PlanRunState) -> str:
        blocked = {
            step_id: state.unsatisfied_dependencies(step_id)
            for step_id in state.unfinished_ids()
            if state.unsatisfied_dependencies(step_id)
        }
        return "\n".join([
            "You are a task-execution agent. Execute the plan step by step using the "
            "available tools, then produce a final answer.",
            "",
            "Plan rules:",
            f"- Every tool call must pass {PLAN_STEP_ARGUMENT}: the plan step it works on.",
            "  A step may only run once its dependencies are complete, and a completed "
            "step cannot be replayed (bind new work to an unfinished step instead).",
            f"- A successful tool call does not finish a step. Once a step's work is really "
            f"done, call {COMPLETE_PLAN_STEP} with the evidence that satisfies its "
            "completion_criteria.",
            f"- Call {REQUEST_REPLAN} only when a tool failed unrecoverably, required data "
            "is missing, completion criteria cannot be met, or the plan's dependencies no "
            "longer hold. Completed steps are kept unchanged.",
            "",
            f"Plan v{state.version} progress:",
            f"- completed: {', '.join(state.completed_ids()) or '(none)'}",
            f"- running: {', '.join(state.running_ids()) or '(none)'}",
            f"- ready (dependencies complete, not finished): {', '.join(state.ready_ids()) or '(none)'}",
            "- blocked: " + (", ".join(
                f"{step_id} (waiting for {', '.join(deps)})" for step_id, deps in blocked.items()
            ) or "(none)"),
        ])

    def _run_tool_call(
        self, task_id: str, state: PlanRunState, call: Any, messages: list[dict[str, Any]]
    ) -> PlanRunState:
        """Execute one tool call, but only for a step the plan currently allows."""
        name = call.function.name
        arguments = self._parse_arguments(call)
        declared: Any = None
        if isinstance(arguments, dict) and PLAN_STEP_ARGUMENT in arguments:
            declared = arguments.pop(PLAN_STEP_ARGUMENT)

        step_id, violation, implicit = self._bind_step(state, declared)
        if violation:
            self._record_plan_event(task_id, {
                "kind": "plan_binding_rejected",
                "step_id": declared if isinstance(declared, str) else None,
                "tool": name,
                "tool_call_id": call.id,
                "version": state.version,
                "violation": violation,
            })
            self._observe(messages, call.id, f"[plan binding rejected] {violation}")
            return state

        step = self._task_manager.add_step(
            task_id, name, tool=name, plan_step_id=step_id, tool_call_id=call.id
        )
        self._task_manager.start_step(task_id, step.id)
        if step_id is None:
            self._record_plan_event(task_id, {
                "kind": "plan_unbound_tool_call",
                "tool": name,
                "tool_call_id": call.id,
                "task_step_id": step.id,
                "version": state.version,
                "candidates": state.candidates_for_implicit_binding(),
            })
        else:
            self._record_plan_event(task_id, {
                "kind": "plan_step_bound",
                "step_id": step_id,
                "tool": name,
                "tool_call_id": call.id,
                "task_step_id": step.id,
                "version": state.version,
                "implicit": implicit,
            })

        result, _, attempts = self._invoke_tool(
            task_id, name, arguments, step_id=step.id, tool_call_id=call.id,
        )
        self._task_manager.set_step_attempts(task_id, step.id, attempts)
        try:
            prepared = self._prepare_tool_output(
                task_id=task_id,
                tool_name=name,
                tool_call_id=call.id,
                result=result,
            )
        except Exception as exc:
            # Failing closed keeps an unpersisted large result out of both the
            # model context and the task JSON.
            self._task_manager.fail_step(
                task_id, step.id, error="tool_output_processing_failed"
            )
            raise ToolOutputProcessingError("tool_output_processing_failed") from exc

        if prepared.truncated:
            self._task_manager.add_metric_events(task_id, "context_events", [{
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "kind": "context_output_offloaded",
                "tool": name,
                "tool_call_id": call.id,
                "task_step_id": step.id,
                "result_ref": prepared.result_ref,
                "size_bytes": prepared.size_bytes,
            }])

        if result.success:
            self._task_manager.succeed_step(
                task_id,
                step.id,
                output=prepared.task_text,
                result_ref=prepared.result_ref,
                result_size_bytes=prepared.size_bytes,
                result_content_type=prepared.content_type,
                result_truncated=prepared.truncated,
            )
            # Only deliverables are artifacts; other tools also attach
            # metadata (parse_document reports chars/markdown_path).
            if result.metadata.get("kind") == "artifact":
                artifact = {
                    key: value
                    for key, value in result.metadata.items()
                    if key != "kind"
                }
                self._task_manager.add_artifact(task_id, artifact)
        else:
            self._task_manager.fail_step(
                task_id,
                step.id,
                error=prepared.task_text,
                result_ref=prepared.result_ref,
                result_size_bytes=prepared.size_bytes,
                result_content_type=prepared.content_type,
                result_truncated=prepared.truncated,
            )
            if step_id is not None:
                # Observation only: a tool error never triggers a replan by itself.
                self._record_plan_event(task_id, {
                    "kind": "plan_step_failed",
                    "step_id": step_id,
                    "version": state.version,
                    "tool": name,
                    "tool_call_id": call.id,
                    "task_step_id": step.id,
                    "error": prepared.task_text,
                    "error_type": result.error_type.value if result.error_type else None,
                })
        if result.terminal:
            raise UnsafeExecutionStateError(prepared.task_text)

        self._observe(messages, call.id, prepared.model_text)
        return self._plan_state(task_id, state.plan)

    def _prepare_tool_output(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_call_id: str | None,
        result: ToolResult,
    ) -> PreparedToolOutput:
        """Run the common post-tool hook before persistence or model feedback."""
        if self._after_tool_call is not None:
            return self._after_tool_call.process(
                task_id=task_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=result,
            )
        raw = result.output if result.success else (result.error or "unknown error")
        return PreparedToolOutput(
            model_text=result.to_text(),
            task_text=raw,
            size_bytes=len(raw.encode("utf-8")),
            content_type="text/plain; charset=utf-8",
        )

    @staticmethod
    def _bind_step(state: PlanRunState, declared: Any) -> tuple[str | None, str | None, bool]:
        """Resolve which planned step a tool call belongs to, plus any violation."""
        if not state.step_ids():
            # No plan to attribute to (only reachable with a bypassed planner):
            # keep the call working and record it as unbound.
            return None, None, False
        if declared is not None:
            if not isinstance(declared, str) or not declared.strip():
                return None, f"{PLAN_STEP_ARGUMENT} must be a non-empty string", False
            if declared not in state.step_ids():
                return None, f"unknown plan step '{declared}' (plan v{state.version})", False
            if state.is_complete(declared):
                return None, (
                    f"plan step '{declared}' is already complete and cannot be replayed; "
                    "bind this call to an unfinished step or request a replan"
                ), False
            waiting = state.unsatisfied_dependencies(declared)
            if waiting:
                return None, (
                    f"plan step '{declared}' is waiting for {', '.join(waiting)}; "
                    "finish the prerequisites first"
                ), False
            return declared, None, False
        candidates = state.candidates_for_implicit_binding()
        if len(candidates) == 1:
            return candidates[0], None, True
        return None, None, False

    def _complete_plan_step(
        self, task_id: str, state: PlanRunState, call: Any, messages: list[dict[str, Any]]
    ) -> PlanRunState:
        arguments = self._parse_arguments(call)
        step_id = arguments.get("step_id") if isinstance(arguments, dict) else None
        evidence = arguments.get("evidence") if isinstance(arguments, dict) else None
        violation = self._completion_violation(state, step_id, evidence)
        if violation:
            self._record_plan_event(task_id, {
                "kind": "plan_completion_rejected",
                "step_id": step_id if isinstance(step_id, str) else None,
                "tool_call_id": call.id,
                "version": state.version,
                "violation": violation,
            })
            self._observe(messages, call.id, f"[plan step rejected] {violation}")
            return state

        step = state.plan.step_by_id(step_id)
        self._record_plan_event(task_id, {
            "kind": "plan_step_completed",
            "step_id": step_id,
            "version": state.version,
            "evidence": evidence,
            "completion_criteria": list(step.completion_criteria),
            "tool_call_id": call.id,
        })
        refreshed = self._plan_state(task_id, state.plan)
        self._observe(
            messages, call.id,
            f"[plan] step '{step_id}' recorded as complete (plan v{state.version}). "
            f"remaining: {', '.join(refreshed.unfinished_ids()) or '(none)'}",
        )
        return refreshed

    @staticmethod
    def _completion_violation(state: PlanRunState, step_id: Any, evidence: Any) -> str | None:
        if not isinstance(step_id, str) or not step_id.strip():
            return "step_id must be a non-empty string"
        if step_id not in state.step_ids():
            return f"unknown plan step '{step_id}' (plan v{state.version})"
        if state.is_complete(step_id):
            return f"plan step '{step_id}' is already complete and cannot be completed again"
        if not isinstance(evidence, str) or not evidence.strip():
            return (
                "evidence must be a non-empty string describing the observable result that "
                "satisfies the step's completion_criteria"
            )
        waiting = state.unsatisfied_dependencies(step_id)
        if waiting:
            return f"plan step '{step_id}' is waiting for {', '.join(waiting)}"
        return None

    def _replan(
        self,
        task_id: str,
        user_input: str,
        state: PlanRunState,
        call: Any,
        messages: list[dict[str, Any]],
        replans_used: int,
        memory_context: str | None,
    ) -> tuple[PlanRunState, int]:
        arguments = self._parse_arguments(call)
        reason_code = arguments.get("reason_code") if isinstance(arguments, dict) else None
        reason = arguments.get("reason") if isinstance(arguments, dict) else None

        violation = self._replan_violation(state, reason_code, reason)
        if violation:
            self._record_plan_event(task_id, {
                "kind": "plan_replan_rejected",
                "tool_call_id": call.id,
                "version": state.version,
                "reason_code": reason_code if isinstance(reason_code, str) else None,
                "violation": violation,
            })
            self._observe(messages, call.id, f"[replan rejected] {violation}")
            return state, replans_used
        if replans_used >= self._max_replans:
            self._record_plan_event(task_id, {
                "kind": "plan_replan_exhausted",
                "tool_call_id": call.id,
                "version": state.version,
                "reason_code": reason_code,
                "reason": reason,
                "used": replans_used,
                "max_replans": self._max_replans,
            })
            raise ReplanBudgetExceededError(
                f"replan budget exhausted: {replans_used} of {self._max_replans} local "
                f"replans used, last request was {reason_code} ({reason})"
            )

        self._record_plan_event(task_id, {
            "kind": "plan_replan_requested",
            "tool_call_id": call.id,
            "version": state.version,
            "reason_code": reason_code,
            "reason": reason,
            "remaining_step_ids": state.unfinished_ids(),
        })
        try:
            replan_arguments = {
                "completed_steps": self._completed_step_records(state),
                "observations": state.failure_observations(),
                "available_tools": [
                    schema["function"]["name"] for schema in self._registry.to_openai_tools()
                ],
            }
            if memory_context:
                replan_arguments["memory_context"] = memory_context
            new_plan = self._planner.replan(user_input, state.plan, **replan_arguments)
            self._task_manager.replace_plan(
                task_id, new_plan, reason_code=reason_code, reason=reason
            )
        except (PlannerError, PlanValidationError, TaskStateError, ValueError) as exc:
            self._record_plan_event(task_id, {
                "kind": "plan_replan_rejected",
                "tool_call_id": call.id,
                "version": state.version,
                "reason_code": reason_code,
                "violation": str(exc),
            })
            self._observe(messages, call.id, f"[replan rejected] the new plan was not applied: {exc}")
            return state, replans_used

        refreshed = self._plan_state(task_id, state.plan)
        self._observe(
            messages, call.id,
            f"[replan applied] plan v{refreshed.version}: "
            f"{', '.join(refreshed.unfinished_ids()) or '(nothing left)'} left to do. "
            f"Steps {', '.join(refreshed.completed_ids()) or '(none)'} stay complete.",
        )
        return refreshed, replans_used + 1

    def _replan_violation(
        self, state: PlanRunState, reason_code: Any, reason: Any
    ) -> str | None:
        if not isinstance(reason_code, str) or reason_code not in REPLAN_REASON_CODES:
            return f"reason_code must be one of: {', '.join(REPLAN_REASON_CODES)}"
        if not isinstance(reason, str) or not reason.strip():
            return "reason must be a non-empty string describing the observation"
        if not state.unfinished_ids():
            return "every plan step is already complete, so there is nothing left to replan"
        if self._planner is None:
            return "replanning is not configured for this executor"
        return None

    @staticmethod
    def _completed_step_records(state: PlanRunState) -> list[dict[str, Any]]:
        records = []
        for step_id in state.completed_ids():
            step = state.plan.step_by_id(step_id)
            records.append({
                "step_id": step.step_id,
                "name": step.name,
                "description": step.description,
                "tool": step.tool,
                "depends_on": list(step.depends_on),
                "completion_criteria": list(step.completion_criteria),
                "evidence": state.evidence(step_id),
            })
        return records

    def _record_plan_event(self, task_id: str, event: dict[str, Any]) -> None:
        self._task_manager.add_plan_events(task_id, [{
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            **event,
        }])

    @staticmethod
    def _parse_arguments(call: Any) -> Any:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except (ValueError, TypeError):
            return None  # Registry produces INVALID_ARGUMENT without invoking a tool.
        return arguments if isinstance(arguments, dict) else arguments

    @staticmethod
    def _observe(messages: list[dict[str, Any]], call_id: str, content: str) -> None:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

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

    @staticmethod
    def _memory_context(memory_context: str | None) -> str:
        if not memory_context:
            return ""
        return f"\n\n{memory_context}"

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
        bounded_error = error_message
        if isinstance(error_message, str) and len(error_message) > MAX_RETRY_EVENT_ERROR_CHARS:
            bounded_error = (
                error_message[:MAX_RETRY_EVENT_ERROR_CHARS]
                + "...[retry event error truncated]"
            )
        return {
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "tool_name": tool_name,
            "attempt": attempt,
            "error_type": error_type.value if error_type else None,
            "error_message": bounded_error,
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
