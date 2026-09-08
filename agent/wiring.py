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
from task.sqlite_store import SQLiteTaskStore
from tools.file_tool import FileTool
from tools.report_tool import ReportTool
from tools.sandbox_tool import SandboxTool
from tools.tool_registry import ToolRegistry
from tools.document_tool import DocumentTool
from tools.history_tool import HistoryTool
from memory.notes import NoteMemory
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

    registry.register(HistoryTool())
    if settings.mcp_parser_url:
        from tools.mcp_document_tool import MCPDocumentTool
        registry.register(MCPDocumentTool(sandbox, settings.mcp_parser_url, settings.mcp_parser_tool))
    else:
        registry.register(DocumentTool(sandbox))

    task_manager = TaskManager(SQLiteTaskStore(settings.task_db_path))
    llm = LLMClient(settings)
    planner = Planner(llm)
    executor = Executor(llm, registry, task_manager, max_iterations=settings.max_iterations,
                        context_max_chars=settings.context_max_chars)
    return AgentOrchestrator(task_manager, planner, executor, sandbox=sandbox,
                             memory=NoteMemory(settings.task_db_path),
                             task_timeout=settings.task_timeout_seconds,
                             max_tool_calls=settings.max_tool_calls,
                             report_prefix=settings.report_prefix)
