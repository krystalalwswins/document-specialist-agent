"""SandboxClient-shaped adapter over the one-shot execution backend.

Tools, hooks and the input stager already speak a small surface
(``execute_python`` / ``read_text_file`` / ``write_bytes_file`` / ...). Keeping
that surface stable means the migration touches the composition root instead of
every tool, while the implementation underneath becomes:

* code execution -> one throwaway container per call (``ContainerSupervisor``),
* file access    -> the host task workspace (``TaskWorkspace``).

There is deliberately no fallback to the legacy HTTP backend: production wiring
builds this adapter only (design note 14 §3.11).
"""

from __future__ import annotations

import posixpath
import uuid
from typing import Optional

from core.config import Settings
from sandbox.client import ExecutionResult
from sandbox.container_supervisor import ContainerSupervisor
from sandbox.task_workspace import TaskWorkspace
from security.permission_manager import PermissionDenied, task_workspace


class OneShotSandboxClient:
    """Host-side replacement for the legacy HTTP ``SandboxClient``."""

    def __init__(
        self,
        settings: Settings,
        supervisor: ContainerSupervisor,
        workspace: TaskWorkspace,
    ) -> None:
        self._settings = settings
        self._supervisor = supervisor
        self._workspace = workspace

    @property
    def workspace(self) -> str:
        return self._workspace.virtual_root

    # -- execution ----------------------------------------------------------
    def execute_python(
        self,
        code: str,
        timeout: Optional[int] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> ExecutionResult:
        if session_id is not None:
            raise ValueError("caller-owned sessions are not supported; use workspace files for state")
        task_id = self._task_id_from(cwd)
        call_id = "call-" + uuid.uuid4().hex[:12]
        outcome = self._supervisor.run(
            code=code,
            task_dir=self._workspace.task_host_dir(task_id),
            task_id=task_id,
            call_id=call_id,
            timeout=timeout,
        )
        return ExecutionResult(
            status=outcome.status,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            error=outcome.error or None,
            outputs=[],
            raw=outcome,
            execution_uncertain=outcome.execution_uncertain,
        )

    # -- files --------------------------------------------------------------
    def resolve(self, filename: str) -> str:
        return self._workspace.to_host(filename).as_posix() if not posixpath.isabs(filename) else filename

    def read_text_file(self, filename: str) -> str:
        try:
            return self._workspace.read_text(filename)
        except FileNotFoundError as exc:
            raise PermissionDenied("file_not_found") from exc

    def read_bytes_file(self, filename: str) -> bytes:
        try:
            return self._workspace.read_bytes(filename)
        except FileNotFoundError as exc:
            raise PermissionDenied("file_not_found") from exc

    def write_text_file(self, filename: str, content: str) -> str:
        self._workspace.write_text(self._as_virtual(filename), content)
        return self._as_virtual(filename)

    def write_bytes_file(self, filename: str, content: bytes) -> str:
        self._workspace.write_bytes(self._as_virtual(filename), content)
        return self._as_virtual(filename)

    def delete_file(self, filename: str) -> None:
        self._workspace.delete(self._as_virtual(filename))

    def ensure_directory(self, filename: str) -> str:
        virtual = self._as_virtual(filename)
        self._workspace.to_host(virtual).mkdir(parents=True, exist_ok=True)
        return virtual

    # -- helpers ------------------------------------------------------------
    def _as_virtual(self, filename: str) -> str:
        """Accept both absolute virtual paths and workspace-relative names."""
        if posixpath.isabs(filename):
            return filename
        return posixpath.join(self._workspace.virtual_root, filename)

    def _task_id_from(self, cwd: Optional[str]) -> str:
        if not cwd:
            raise PermissionDenied("task_context_required")
        tasks_root = posixpath.join(self._workspace.virtual_root, "tasks") + "/"
        if not cwd.startswith(tasks_root):
            raise PermissionDenied("outside_workspace")
        task_id = cwd[len(tasks_root) :].split("/", 1)[0]
        # Re-validate through the shared helper (raises on a malformed id).
        task_workspace(self._workspace.virtual_root, task_id)
        return task_id
