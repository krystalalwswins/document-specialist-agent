"""Deterministic Fake LLM runtime for repeatable Agent evaluation.

Only the provider and external tools are faked. Cases still travel through the
real Orchestrator, Executor, ToolRegistry, retry policy, hooks and TaskManager.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.executor import Executor
from agent.orchestrator import AgentOrchestrator
from agent.validator import ArtifactCheck, ValidationResult
from context.hooks import AfterToolCallHook
from context.tool_output_store import ToolOutputStore
from memory.model import MemoryRecord, MemorySourceKind, MemoryStatus, MemoryType
from memory.service import MemoryCaptureReport
from retry.retry_policy import RetryPolicy
from security.permission_manager import PermissionManager
from task.plan_model import Plan
from task.task_manager import TaskManager
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.tool_output_tool import ReadToolOutputTool
from tools.tool_registry import ToolRegistry

from .model import EvaluationCase
from .runner import CaseExecution, OrchestratorCaseExecutor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(message: Any, total_tokens: int) -> Any:
    prompt_tokens = max(1, total_tokens * 3 // 4)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=total_tokens - prompt_tokens,
            total_tokens=total_tokens,
        ),
    )


class ScriptedPlanner:
    def __init__(self, initial: Plan, replans: list[Plan]) -> None:
        self._initial = initial
        self._replans = list(replans)
        self.memory_contexts: list[str | None] = []

    def plan(self, user_input: str, on_event=None, *, memory_context=None) -> Plan:
        if user_input != self._initial.user_input:
            raise ValueError("fake plan goal does not match the case")
        self.memory_contexts.append(memory_context)
        return self._initial

    def replan(
        self,
        user_input,
        plan,
        *,
        completed_steps,
        observations,
        available_tools,
        memory_context=None,
        on_event=None,
    ) -> Plan:
        if not self._replans:
            raise ValueError("fake case requested an undeclared replan")
        self.memory_contexts.append(memory_context)
        return self._replans.pop(0)


class ScriptedLLM:
    model = "scripted-fake"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        if not self._responses:
            raise RuntimeError("fake LLM response script exhausted")
        self.calls.append(copy.deepcopy(messages))
        spec = self._responses.pop(0)
        calls = []
        for index, raw in enumerate(spec.get("tool_calls", []), start=1):
            arguments = self._replace_dynamic(raw.get("arguments", {}), messages)
            calls.append(
                _tool_call(
                    raw["name"],
                    arguments,
                    raw.get("id", f"fake_call_{len(self.calls)}_{index}"),
                )
            )
        message = SimpleNamespace(
            content=spec.get("content"),
            tool_calls=calls,
        )
        total_tokens = int(spec.get("total_tokens", 100))
        response = _response(message, total_tokens)
        if on_event is not None:
            on_event({
                "occurred_at": _now(),
                "model": self.model,
                "attempt": 1,
                "error_type": None,
                "error_message": None,
                "retry_reason": "success",
                "duration_ms": 1,
                "final_status": "SUCCESS",
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            })
        return response

    def _replace_dynamic(self, value: Any, messages: list[dict[str, Any]]) -> Any:
        if value == "$last_result_ref":
            return self._last_result_ref(messages)
        if isinstance(value, dict):
            return {key: self._replace_dynamic(item, messages) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_dynamic(item, messages) for item in value]
        return value

    @staticmethod
    def _last_result_ref(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message.get("content") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            result_ref = payload.get("result_ref") if isinstance(payload, dict) else None
            if isinstance(result_ref, str):
                return result_ref
        raise RuntimeError("fake response requested result_ref before an offloaded result")


class ScriptedTool(BaseTool):
    description = "Deterministic evaluation tool"

    def __init__(
        self,
        name: str,
        outcomes: list[dict[str, Any]],
        *,
        retry_safe: bool,
    ) -> None:
        self.name = name
        self.retry_safe = retry_safe
        self._outcomes = list(outcomes or [{"success": True, "output": f"{name} ok"}])
        self.calls = 0

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    def execute(self, value: str) -> ToolResult:
        self.calls += 1
        raw = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        output = str(raw.get("output", ""))
        repeated = raw.get("output_repeat")
        if isinstance(repeated, dict):
            output = (
                str(repeated.get("text", "x")) * int(repeated.get("count", 1))
                + str(repeated.get("suffix", ""))
            )
        error_type = raw.get("error_type")
        return ToolResult(
            success=bool(raw.get("success", True)),
            output=output,
            error=raw.get("error"),
            error_type=ErrorType(error_type) if error_type else None,
            terminal=bool(raw.get("terminal", False)),
            metadata=dict(raw.get("metadata", {})),
        )


class ScriptedArtifactValidator:
    def __init__(self, results: list[bool]) -> None:
        self._results = list(results or [True])

    def validate(self, artifacts) -> ValidationResult:
        ok = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        return ValidationResult(
            ok=ok,
            checks=[
                ArtifactCheck(
                    str(artifact.get("oss_key", "")),
                    ok,
                    "ok" if ok else "scripted artifact validation failure",
                    int(artifact.get("bytes", 0)),
                )
                for artifact in artifacts
            ],
        )


class StaticMemoryService:
    def __init__(self, case: EvaluationCase, config: dict[str, Any]) -> None:
        content = str(config.get("content", "Prefer concise reports"))
        self._record = MemoryRecord(
            id=str(config.get("id", "eval-memory-1")),
            user_id=case.user_id,
            project_id=case.project_id,
            memory_type=MemoryType(str(config.get("type", "PREFERENCE"))),
            content=content,
            normalized_content=content.casefold(),
            content_hash=hashlib.sha256(content.casefold().encode()).hexdigest(),
            source_task_id=str(config.get("source_task_id", "eval-source-task")),
            source_kind=MemorySourceKind.USER_EXPLICIT,
            source_excerpt=content,
            confidence=float(config.get("confidence", 0.95)),
            status=MemoryStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
        )

    def recall(self, **kwargs):
        return [self._record]

    def context(self, records) -> str:
        record = records[0]
        return (
            "Relevant long-term memory:\n"
            f"- [{record.memory_type.value}] {record.content} "
            f"(source_task_id={record.source_task_id})"
        )

    def capture(self, task, answer, on_event=None) -> MemoryCaptureReport:
        return MemoryCaptureReport()


class FakeCaseExecutor:
    """Build a fresh deterministic Harness for every case."""

    def __init__(self, work_dir: str | Path) -> None:
        self._work_dir = Path(work_dir)

    def execute(self, case: EvaluationCase) -> CaseExecution:
        orchestrator = self._build(case)
        return OrchestratorCaseExecutor(orchestrator).execute(case)

    def _build(self, case: EvaluationCase) -> AgentOrchestrator:
        config = case.fake
        if not config:
            raise ValueError(f"fake evaluation case '{case.id}' has no fake script")
        plan = self._plan(case.user_input, config.get("plan", []), version=1)
        replans = [
            self._plan(case.user_input, steps, version=index + 2)
            for index, steps in enumerate(config.get("replans", []))
        ]
        planner = ScriptedPlanner(plan, replans)
        llm = ScriptedLLM(list(config.get("responses", [])))
        manager = TaskManager()
        permissions = PermissionManager(
            allowed_permissions=frozenset({
                "file.read",
                "artifact.write",
                "sandbox.execute",
                "tool_output.read",
                "memory.read",
            })
        )
        registry = ToolRegistry(permissions)
        for tool in config.get("tools", []):
            registry.register(
                ScriptedTool(
                    str(tool["name"]),
                    list(tool.get("outcomes", [])),
                    retry_safe=bool(tool.get("retry_safe", False)),
                )
            )

        case_dir = self._work_dir / hashlib.sha256(case.id.encode()).hexdigest()[:16]
        output_store = ToolOutputStore(case_dir / "tool-output", max_read_chars=120)
        if self._uses_tool(config, "read_tool_output"):
            registry.register(ReadToolOutputTool(output_store))
        hook = AfterToolCallHook(output_store, inline_chars=400, preview_chars=80)
        executor = Executor(
            llm,
            registry,
            manager,
            max_iterations=12,
            retry_policy=RetryPolicy(
                max_attempts=3,
                base_delay=0,
                max_delay=0,
                jitter=False,
            ),
            planner=planner,
            max_replans=2,
            after_tool_call=hook,
        )
        validator = None
        if "artifact_validation" in config:
            validator = ScriptedArtifactValidator(
                [bool(value) for value in config["artifact_validation"]]
            )
        memory = None
        if isinstance(config.get("memory"), dict):
            memory = StaticMemoryService(case, config["memory"])
        return AgentOrchestrator(
            manager,
            planner,
            executor,
            validator=validator,
            max_recovery_attempts=int(config.get("max_recovery_attempts", 0)),
            memory_service=memory,
        )

    @staticmethod
    def _plan(user_input: str, steps: list[dict[str, Any]], *, version: int) -> Plan:
        return Plan.from_dict({
            "user_input": user_input,
            "version": version,
            "steps": steps,
        })

    @staticmethod
    def _uses_tool(config: dict[str, Any], name: str) -> bool:
        return any(
            call.get("name") == name
            for response in config.get("responses", [])
            for call in response.get("tool_calls", [])
        )
