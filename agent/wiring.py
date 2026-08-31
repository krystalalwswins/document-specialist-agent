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


def build_orchestrator(settings: Settings | None = None) -> AgentOrchestrator:
    settings = settings or get_settings()

    sandbox = SandboxClient(settings)
    storage = StorageManager(settings)

    registry = ToolRegistry()
    registry.register(SandboxTool(sandbox))
    registry.register(FileTool(sandbox))
    registry.register(ReportTool(sandbox, storage))

    task_manager = TaskManager()
    llm = LLMClient(settings)
    planner = Planner(llm)
    executor = Executor(llm, registry, task_manager)
    return AgentOrchestrator(task_manager, planner, executor)
