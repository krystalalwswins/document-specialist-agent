"""Object storage layer (MinIO / S3-compatible)."""

from .storage_manager import StorageError, StorageManager

__all__ = ["StorageError", "StorageManager"]
