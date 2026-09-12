"""InputStager: load a task's inputs from object storage into the sandbox.

The API accepts object keys, not file bytes: the user uploads a document once and
several tasks can reuse it. Staging happens before planning so the model is told
the exact absolute paths that already exist inside the sandbox.

Two guards keep a submitted key from escaping its intended scope:
``input_prefix`` confines reads to one object-storage prefix, and
``workspace_path`` rejects traversal/absolute tricks in the destination name.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Optional

from security.permission_manager import object_key, workspace_path
from storage.storage_manager import StorageError
from core.config import Settings, get_settings


class InputStagingError(Exception):
    """Raised when a declared input cannot be staged into the sandbox."""


class InputStager:
    def __init__(self, storage: Any, sandbox: Any, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._storage = storage
        self._sandbox = sandbox

    def stage(self, oss_key: str, filename: Optional[str] = None) -> dict[str, Any]:
        """Copy one object into the sandbox workspace; return the staging record."""
        key = object_key(self._settings.input_prefix, oss_key)
        name = filename or PurePosixPath(key).name
        path = workspace_path(self._settings.sandbox_workspace, name)

        try:
            data = self._storage.download_file_content(key)
        except StorageError as exc:
            raise InputStagingError(f"cannot load input '{key}': {exc}") from exc
        if not data:
            raise InputStagingError(f"input '{key}' is empty")

        try:
            self._sandbox.write_bytes_file(name, data)
        except Exception as exc:  # sandbox/SDK failure -> task-level staging failure
            raise InputStagingError(f"cannot stage '{key}' into the sandbox: {exc}") from exc
        return {
            "oss_key": key,
            "sandbox_path": path,
            "filename": PurePosixPath(path).name,
            "bytes": len(data),
        }

    def stage_all(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stage every declared input, preserving order and original fields."""
        staged: list[dict[str, Any]] = []
        for spec in specs:
            record = self.stage(spec["oss_key"], spec.get("filename"))
            staged.append({**spec, **record})
        return staged
