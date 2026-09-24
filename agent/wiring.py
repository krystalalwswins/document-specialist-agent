"""Composition root: assemble the agent and its dependencies from settings."""

from __future__ import annotations

from agent.executor import Executor
from agent.llm_client import LLMClient
from agent.orchestrator import AgentOrchestrator
from agent.planner import Planner
from agent.validator import ArtifactValidator
from context.hooks import AfterToolCallHook
from context.compactor import ContextCompactor
from context.manager import ContextManager
from context.token_estimator import TokenEstimator
from context.tool_output_store import ToolOutputStore
from core.config import Settings, get_settings
from memory.extractor import MemoryExtractor
from memory.policy import MemoryPolicy
from memory.service import MemoryService
from memory.store import SQLiteMemoryStore
from sandbox.client import SandboxClient
from sandbox.inputs import InputStager
from storage.storage_manager import StorageManager
from task.file_task_store import FileTaskStore
from task.task_manager import TaskManager
from tools.document_tool import ParseDocumentTool
from tools.file_tool import FileTool
from tools.memory_tool import SearchMemoryTool
from tools.report_tool import ReportTool
from tools.sandbox_tool import SandboxTool
from tools.tool_registry import ToolRegistry
from tools.tool_output_tool import ReadToolOutputTool
from security.permission_manager import PermissionManager


def build_orchestrator(settings: Settings | None = None) -> AgentOrchestrator:
    settings = settings or get_settings()

    sandbox = SandboxClient(settings)
    storage = StorageManager(settings)
    task_manager = TaskManager(FileTaskStore(settings.task_store_dir))
    llm = LLMClient(settings)

    memory_service = None
    if settings.memory_enabled:
        memory_service = MemoryService(
            SQLiteMemoryStore(settings.memory_db_path),
            MemoryExtractor(llm),
            MemoryPolicy(
                min_confidence=settings.memory_min_confidence,
                max_content_chars=settings.memory_max_content_chars,
            ),
            recall_top_k=settings.memory_recall_top_k,
        )

    registry = ToolRegistry(PermissionManager(
        workspace=settings.sandbox_workspace,
        report_prefix=settings.report_prefix,
        allowed_tools=frozenset(settings.allowed_tools),
        allowed_permissions=frozenset(settings.allowed_permissions),
    ))
    registry.register(SandboxTool(sandbox, max_timeout=settings.sandbox_max_timeout))
    registry.register(FileTool(sandbox))
    registry.register(ParseDocumentTool(sandbox, max_chars=settings.document_max_chars))
    registry.register(ReportTool(sandbox, storage, report_prefix=settings.report_prefix))

    tool_output_store = ToolOutputStore(
        settings.tool_output_store_dir,
        max_read_chars=settings.tool_output_read_max_chars,
    )
    registry.register(ReadToolOutputTool(tool_output_store))
    if memory_service is not None:
        registry.register(SearchMemoryTool(memory_service, task_manager))
    after_tool_call = AfterToolCallHook(
        tool_output_store,
        inline_chars=settings.tool_output_inline_chars,
        preview_chars=settings.tool_output_preview_chars,
    )

    planner = Planner(llm)
    context_manager = ContextManager(
        TokenEstimator(settings.llm_model),
        ContextCompactor(
            llm, max_attempts=settings.context_compaction_max_attempts
        ),
        soft_limit=settings.context_soft_limit_tokens,
        hard_limit=settings.context_hard_limit_tokens,
        target_tokens=settings.context_target_tokens,
        recent_groups=settings.context_recent_groups,
        failure_threshold=settings.context_compaction_failure_threshold,
        summary_max_chars=settings.context_summary_max_chars,
    )
    executor = Executor(
        llm,
        registry,
        task_manager,
        planner=planner,
        after_tool_call=after_tool_call,
        context_manager=context_manager,
    )
    return AgentOrchestrator(
        task_manager,
        planner,
        executor,
        input_stager=InputStager(storage, sandbox, settings),
        validator=ArtifactValidator(storage),
        max_recovery_attempts=settings.task_max_recovery_attempts,
        memory_service=memory_service,
    )
