"""Centralized, typed application configuration.

All settings load from environment variables or a local `.env` file via
pydantic-settings. Use `get_settings()` instead of importing module-level
globals: it is cached, and importing this module has no side effects.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # AIO Sandbox
    sandbox_base_url: str = "http://localhost:8080"
    sandbox_workspace: str = "/home/gem/workspace"
    sandbox_api_key: str = ""
    sandbox_default_timeout: int = Field(default=30, ge=1)
    sandbox_max_timeout: int = Field(default=120, ge=1)
    sandbox_http_grace: int = Field(default=10, ge=1, le=60)
    allowed_tools: list[str] = Field(
        default_factory=lambda: [
            "run_python",
            "read_file",
            "parse_document",
            "save_report",
            "read_tool_output",
            "search_memory",
        ]
    )
    allowed_permissions: list[str] = Field(
        default_factory=lambda: [
            "sandbox.execute",
            "file.read",
            "artifact.write",
            "tool_output.read",
            "memory.read",
        ]
    )
    report_prefix: str = "reports"
    # Object-storage prefix that task inputs may be loaded from (OSS -> sandbox).
    input_prefix: str = Field(default="raw", min_length=1)

    @model_validator(mode="after")
    def check_security_settings(self):
        from security.permission_manager import object_key, workspace_path
        if self.sandbox_default_timeout > self.sandbox_max_timeout:
            raise ValueError("default timeout must not exceed max timeout")
        if self.llm_retry_base_delay > self.llm_retry_max_delay:
            raise ValueError("llm retry base delay must not exceed max delay")
        if self.tool_output_preview_chars > self.tool_output_inline_chars:
            raise ValueError("tool output preview must not exceed inline threshold")
        if self.tool_output_read_max_chars > self.tool_output_inline_chars // 2:
            raise ValueError("tool output read limit must not exceed half the inline threshold")
        if not (
            self.context_target_tokens
            < self.context_soft_limit_tokens
            < self.context_hard_limit_tokens
        ):
            raise ValueError("context limits must satisfy target < soft < hard")
        if self.context_hard_limit_tokens + self.context_output_reserve_tokens > self.context_window_tokens:
            raise ValueError("context hard limit plus output reserve exceeds model window")
        if self.task_soft_token_budget >= self.task_hard_token_budget:
            raise ValueError("task token budget must satisfy soft < hard")
        pricing = (
            self.llm_input_price_usd_per_million,
            self.llm_cached_input_price_usd_per_million,
            self.llm_output_price_usd_per_million,
        )
        pricing_version = self.llm_pricing_version.strip()
        if pricing_version or any(rate is not None for rate in pricing):
            if not pricing_version or any(rate is None for rate in pricing):
                raise ValueError(
                    "LLM pricing requires version plus input, cached-input and output rates"
                )
        workspace_path(self.sandbox_workspace, ".", allow_root=True)
        object_key(self.report_prefix, self.report_prefix.rstrip("/") + "/probe")
        object_key(self.input_prefix, self.input_prefix.rstrip("/") + "/probe")
        return self

    # MinIO / S3-compatible object storage
    minio_endpoint: str = "http://localhost:9000"
    # Optional: the address a download link must use for *other* machines.
    # Presigned URLs are signed for the host they are generated with, so this
    # builds a second client for signing instead of rewriting the URL afterwards.
    minio_public_endpoint: str = ""
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadminpassword"
    minio_bucket_name: str = "doc-agent-storage"

    # HTTP API access token. Empty means auth is disabled (local development only):
    # anyone who can reach the port can submit tasks that execute code in the sandbox.
    api_token: str = ""

    # LLM (OpenAI-compatible, e.g. DeepSeek)
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    # Per-attempt HTTP timeout and the retry budget around a single chat call.
    # The SDK default (10 min, 2 hidden retries) can leave a task stuck in RUNNING.
    llm_timeout: float = Field(default=60.0, gt=0)
    llm_max_attempts: int = Field(default=3, ge=1)
    llm_retry_base_delay: float = Field(default=1.0, ge=0)
    llm_retry_max_delay: float = Field(default=10.0, ge=0)
    # Optional, versioned local estimate. Prices change independently of code, so
    # there is deliberately no baked-in provider price that can silently go stale.
    llm_pricing_version: str = ""
    llm_input_price_usd_per_million: Decimal | None = Field(default=None, ge=0)
    llm_cached_input_price_usd_per_million: Decimal | None = Field(default=None, ge=0)
    llm_output_price_usd_per_million: Decimal | None = Field(default=None, ge=0)

    # Task persistence + stale-run recovery (a process restart must not lose history).
    task_store_dir: str = Field(default=".data/tasks", min_length=1)
    # Only tasks with no progress for this long are marked FAILED after a restart.
    # Keep it well above the slowest single step (LLM attempts + tool timeouts).
    task_stale_after_seconds: int = Field(default=1800, ge=1)
    task_reaper_interval_seconds: int = Field(default=60, ge=1)
    # Bounded worker pool: a single sandbox container should not run unbounded
    # tasks at once, and POST /tasks returns 429 once the queue is full.
    task_max_workers: int = Field(default=2, ge=1)
    task_max_pending: int = Field(default=32, ge=0)
    # How many extra attempts a task gets after artifact validation fails.
    task_max_recovery_attempts: int = Field(default=1, ge=0)
    # Cumulative tokens in plan/replan/compaction/execute phases. The soft limit
    # asks ContextManager to converge; the hard limit terminates core execution.
    task_soft_token_budget: int = Field(default=200000, ge=1)
    task_hard_token_budget: int = Field(default=300000, ge=2)

    # Large tool outputs stay local and are recalled through opaque, task-bound
    # references. These limits are character budgets; P0-4 adds token budgeting.
    tool_output_store_dir: str = Field(default=".data/tool_outputs", min_length=1)
    tool_output_inline_chars: int = Field(default=16000, ge=1000)
    tool_output_preview_chars: int = Field(default=2000, ge=100)
    tool_output_read_max_chars: int = Field(default=4000, ge=100)

    # Context input budget. The hard input limit leaves explicit room for the
    # model's answer; P0-4 compacts at the soft limit and aims for the target.
    context_window_tokens: int = Field(default=64000, ge=1000)
    context_output_reserve_tokens: int = Field(default=8000, ge=1)
    context_soft_limit_tokens: int = Field(default=48000, ge=1)
    context_hard_limit_tokens: int = Field(default=56000, ge=1)
    context_target_tokens: int = Field(default=32000, ge=1)
    context_recent_groups: int = Field(default=2, ge=0)
    context_summary_max_chars: int = Field(default=6000, ge=500)
    context_compaction_max_attempts: int = Field(default=2, ge=1)
    context_compaction_failure_threshold: int = Field(default=2, ge=1)

    # Local long-term memory. SQLite is deliberately separate from JSON task
    # history: memory is cross-task knowledge, while TaskStore is execution audit.
    memory_enabled: bool = True
    memory_db_path: str = Field(default=".data/memory/memory.db", min_length=1)
    memory_recall_top_k: int = Field(default=5, ge=1, le=20)
    memory_min_confidence: float = Field(default=0.8, ge=0, le=1)
    memory_max_content_chars: int = Field(default=800, ge=50, le=4000)

    # Document parsing budget (characters returned to the model per call).
    document_max_chars: int = Field(default=20000, ge=100)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton (import-safe, no side effects)."""
    return Settings()
