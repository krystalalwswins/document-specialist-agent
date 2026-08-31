"""Deprecated compatibility shim.

Use `from core.config import get_settings` in new code. This module keeps the
original constant names working until the sandbox/agent modules are migrated,
then it will be removed.
"""

from core.config import get_settings

_settings = get_settings()

SANDBOX_BASE_URL = _settings.sandbox_base_url
SANDBOX_WORKSPACE = _settings.sandbox_workspace
SANDBOX_API_KEY = _settings.sandbox_api_key
MINIO_ENDPOINT = _settings.minio_endpoint
MINIO_ACCESS_KEY = _settings.minio_access_key
MINIO_SECRET_KEY = _settings.minio_secret_key
MINIO_BUCKET_NAME = _settings.minio_bucket_name
LLM_API_KEY = _settings.llm_api_key
LLM_BASE_URL = _settings.llm_base_url
LLM_MODEL = _settings.llm_model
