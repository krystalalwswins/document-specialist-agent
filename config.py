import os

# 云沙箱配置
SANDBOX_BASE_URL = os.getenv("SANDBOX_BASE_URL", "http://localhost:8080")
SANDBOX_WORKSPACE = "/home/gem/workspace"

# MinIO / OSS 对象存储配置
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadminpassword")
MINIO_BUCKET_NAME = os.getenv("MINIO_BUCKET_NAME", "doc-agent-storage")

# 大模型 API 配置 (支持 OpenAI / DeepSeek 等)
LLM_API_KEY = os.getenv("LLM_API_KEY", "your-api-key-here")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")