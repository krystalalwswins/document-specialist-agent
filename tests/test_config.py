"""Settings tests: defaults, env override, cached accessor."""

import pytest
from pydantic import ValidationError

from core.config import Settings, get_settings


def test_defaults(monkeypatch):
    for var in (
        "SANDBOX_BASE_URL",
        "SANDBOX_WORKSPACE",
        "SANDBOX_API_KEY",
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_BUCKET_NAME",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT",
        "LLM_MAX_ATTEMPTS",
        "TASK_STORE_DIR",
        "API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)
    assert settings.sandbox_base_url == "http://localhost:8080"
    assert settings.sandbox_workspace == "/home/gem/workspace"
    assert settings.minio_bucket_name == "doc-agent-storage"
    assert settings.llm_api_key == ""
    assert settings.llm_model == "deepseek-chat"
    assert settings.llm_timeout == 60.0
    assert settings.llm_max_attempts == 3
    assert settings.task_store_dir == ".data/tasks"
    assert settings.task_stale_after_seconds == 1800
    assert settings.api_token == ""


def test_env_override(monkeypatch):
    monkeypatch.setenv("MINIO_BUCKET_NAME", "custom-bucket")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    settings = Settings(_env_file=None)
    assert settings.minio_bucket_name == "custom-bucket"
    assert settings.llm_model == "gpt-test"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_llm_retry_window_is_validated():
    with pytest.raises(ValidationError, match="must not exceed"):
        Settings(_env_file=None, llm_retry_base_delay=30, llm_retry_max_delay=5)


def test_task_store_dir_must_not_be_empty():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, task_store_dir="")
