"""Host-side backing store for the sandbox workspace.

Tools keep speaking POSIX virtual paths (``/home/gem/workspace/tasks/<id>/...``)
because ``PermissionManager`` and ``ToolRegistry`` validate that form. With the
one-shot backend the files actually live on the host, so this class owns the
virtual -> host mapping and the host-side file operations that used to go over
the sandbox HTTP file API (design note 14 §3.2).

Validation reuses ``security.permission_manager`` so traversal rules stay in one
place, then rejects anything that would land outside ``<host_root>/tasks``.
"""

from __future__ import annotations

import os
from pathlib import Path

from security.permission_manager import PermissionDenied, task_workspace, workspace_path


class TaskWorkspace:
    def __init__(self, *, virtual_root: str, host_root: str | Path) -> None:
        self._virtual_root = virtual_root
        self._host_root = Path(host_root)
        # Validate the virtual root once, using the same rules as the tools.
        workspace_path(virtual_root, ".", allow_root=True)

    @property
    def virtual_root(self) -> str:
        return self._virtual_root

    @property
    def host_root(self) -> Path:
        return self._host_root

    def task_virtual_dir(self, task_id: str) -> str:
        return task_workspace(self._virtual_root, task_id)

    def task_host_dir(self, task_id: str) -> Path:
        virtual = self.task_virtual_dir(task_id)
        return self.to_host(virtual)

    def ensure_task_dir(self, task_id: str) -> Path:
        directory = self.task_host_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def to_host(self, virtual_path: str) -> Path:
        """Map a validated virtual path onto the host workspace root."""
        safe = workspace_path(self._virtual_root, virtual_path)
        relative = safe[len(self._virtual_root) :].lstrip("/")
        host = (self._host_root / relative) if relative else self._host_root
        self._ensure_inside(host)
        return host

    # -- host-side file operations -----------------------------------------
    def read_bytes(self, virtual_path: str) -> bytes:
        return self.to_host(virtual_path).read_bytes()

    def read_text(self, virtual_path: str) -> str:
        return self.to_host(virtual_path).read_text(encoding="utf-8")

    def write_text(self, virtual_path: str, content: str) -> Path:
        return self.write_bytes(virtual_path, content.encode("utf-8"))

    def write_bytes(self, virtual_path: str, content: bytes) -> Path:
        target = self.to_host(virtual_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_inside(target)
        temporary = target.with_name(target.name + ".doc-agent-tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
        return target

    def delete(self, virtual_path: str) -> None:
        target = self.to_host(virtual_path)
        if target.exists():
            target.unlink()

    def exists(self, virtual_path: str) -> bool:
        return self.to_host(virtual_path).exists()

    def _ensure_inside(self, path: Path) -> None:
        root = os.path.realpath(str(self._host_root))
        target = os.path.realpath(str(path))
        if os.path.commonpath([root, target]) != root:
            raise PermissionDenied("outside_workspace")
