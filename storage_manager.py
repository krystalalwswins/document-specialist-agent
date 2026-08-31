"""Deprecated compatibility shim.

Use `from storage.storage_manager import StorageManager` in new code. Kept so
the existing hermes_hooks.py keeps working until the sandbox module migration.
"""

from storage.storage_manager import StorageError, StorageManager

__all__ = ["StorageError", "StorageManager"]
