"""Centralized, typed application configuration.

All settings load from environment variables or a local `.env` file via
pydantic-settings. Use `get_settings()` instead of importing module-level
globals: it is cached, and importing this module has no side effects.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # MinIO / S3-compatible object storage
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadminpassword"
    minio_bucket_name: str = "doc-agent-storage"

    # LLM (OpenAI-compatible, e.g. DeepSeek)
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton (import-safe, no side effects)."""
    return Settings()
