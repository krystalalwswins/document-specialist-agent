"""Settings tests: defaults, env override, cached accessor."""

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
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)
    assert settings.sandbox_base_url == "http://localhost:8080"
    assert settings.sandbox_workspace == "/home/gem/workspace"
    assert settings.minio_bucket_name == "doc-agent-storage"
    assert settings.llm_api_key == ""
    assert settings.llm_model == "deepseek-chat"


def test_env_override(monkeypatch):
    monkeypatch.setenv("MINIO_BUCKET_NAME", "custom-bucket")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    settings = Settings(_env_file=None)
    assert settings.minio_bucket_name == "custom-bucket"
    assert settings.llm_model == "gpt-test"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
