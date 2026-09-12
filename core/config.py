"""Centralized, typed application configuration.

All settings load from environment variables or a local `.env` file via
pydantic-settings. Use `get_settings()` instead of importing module-level
globals: it is cached, and importing this module has no side effects.
"""

from __future__ import annotations

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
    allowed_tools: list[str] = Field(default_factory=lambda: ["run_python", "read_file", "save_report"])
    allowed_permissions: list[str] = Field(default_factory=lambda: ["sandbox.execute", "file.read", "artifact.write"])
    report_prefix: str = "reports"

    @model_validator(mode="after")
    def check_security_settings(self):
        from security.permission_manager import object_key, workspace_path
        if self.sandbox_default_timeout > self.sandbox_max_timeout:
            raise ValueError("default timeout must not exceed max timeout")
        workspace_path(self.sandbox_workspace, ".", allow_root=True)
        object_key(self.report_prefix, self.report_prefix.rstrip("/") + "/probe")
        return self

    # MinIO / S3-compatible object storage
    minio_endpoint: str = "http://localhost:9000"
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton (import-safe, no side effects)."""
    return Settings()
