"""Composition root: assemble the agent and its dependencies from settings."""

from __future__ import annotations

from agent.executor import Executor
from agent.llm_client import LLMClient
from agent.orchestrator import AgentOrchestrator
from agent.planner import Planner
from core.config import Settings, get_settings
from sandbox.client import SandboxClient
from storage.storage_manager import StorageManager
from task.task_manager import TaskManager
from tools.file_tool import FileTool
from tools.report_tool import ReportTool
from tools.sandbox_tool import SandboxTool
from tools.tool_registry import ToolRegistry
from security.permission_manager import PermissionManager


def build_orchestrator(settings: Settings | None = None) -> AgentOrchestrator:
    settings = settings or get_settings()

    sandbox = SandboxClient(settings)
    storage = StorageManager(settings)

    registry = ToolRegistry(PermissionManager(
        workspace=settings.sandbox_workspace,
        report_prefix=settings.report_prefix,
        allowed_tools=frozenset(settings.allowed_tools),
        allowed_permissions=frozenset(settings.allowed_permissions),
    ))
    registry.register(SandboxTool(sandbox, max_timeout=settings.sandbox_max_timeout))
    registry.register(FileTool(sandbox))
    registry.register(ReportTool(sandbox, storage, report_prefix=settings.report_prefix))

    task_manager = TaskManager()
    llm = LLMClient(settings)
    planner = Planner(llm)
    executor = Executor(llm, registry, task_manager)
    return AgentOrchestrator(task_manager, planner, executor)
