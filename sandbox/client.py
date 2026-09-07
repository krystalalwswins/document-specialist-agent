"""SandboxClient: thin, testable wrapper over agent_sandbox.Sandbox.

Facts confirmed from the installed SDK source (agent-sandbox 0.0.30):
- write_file supports binary via ``encoding="base64"`` (FileContentEncoding)
- read_file returns text only; download_file returns Iterator[bytes] for binary
- jupyter.execute_code returns ``.data.status`` + ``.data.outputs`` with output_type in
  stream / execute_result / display_data / error
- API key auth is passed via headers (X-AIO-API-Key), not a constructor kwarg
"""

from __future__ import annotations

import base64
import shlex
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from agent_sandbox import Sandbox

from core.config import Settings, get_settings
from security.permission_manager import PermissionDenied, workspace_path


class SandboxError(Exception):
    """Raised when a sandbox operation fails."""


class SandboxExecutionUncertain(SandboxError):
    """A request/cleanup failed; retrying generated code may duplicate side effects."""

    terminal = True


# Runs inside the Linux sandbox, never against the agent host's filesystem.
# This catches existing symlinks. Concurrent replacement remains a documented TOCTOU limitation.
PATH_GUARD = """
import os, stat, sys
from pathlib import Path
root, target = map(Path, sys.argv[1:3])
for path in [target, *target.parents]:
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            sys.exit(73)
    except FileNotFoundError:
        pass
if not root.is_dir():
    sys.exit(74)
if os.path.commonpath([str(root.resolve()), str(target.resolve())]) != str(root.resolve()):
    sys.exit(73)
"""


@dataclass
class ExecutionResult:
    """Normalized, SDK-agnostic result of running code in the sandbox."""

    status: str  # ok / error / timeout
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    traceback: Optional[str] = None
    outputs: list[str] = field(default_factory=list)
    raw: Optional[Any] = None
    execution_uncertain: bool = False

    @property
    def text(self) -> str:
        """Human-readable combined output, suitable to feed back to the LLM."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout)
        parts.extend(self.outputs)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        if self.error:
            parts.append(f"[error] {self.error}")
        if self.traceback:
            parts.append(self.traceback)
        return "\n".join(parts)


class SandboxClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        client: Optional[Any] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or self._build_client()

    def _build_client(self) -> Any:
        headers: Optional[dict[str, str]] = None
        if self._settings.sandbox_api_key:
            headers = {"X-AIO-API-Key": self._settings.sandbox_api_key}
        return Sandbox(base_url=self._settings.sandbox_base_url, headers=headers)

    @property
    def workspace(self) -> str:
        return self._settings.sandbox_workspace

    def resolve(self, filename: str) -> str:
        """Return an absolute path inside the sandbox workspace."""
        return workspace_path(self.workspace, filename)

    def _request_options(self, timeout: int = 10) -> dict[str, Any]:
        return {"timeout_in_seconds": timeout + self._settings.sandbox_http_grace, "max_retries": 0}

    def _shell(self, command: str) -> Any:
        response = self._client.shell.exec_command(
            command=command, timeout=10, hard_timeout=10, request_options=self._request_options(),
        )
        data = getattr(response, "data", None)
        if getattr(response, "success", None) is False or data is None or getattr(data, "exit_code", None) is None:
            raise SandboxError("sandbox path operation did not complete")
        return data

    def _guard_path(self, path: str) -> None:
        command = "python3 -I -c " + shlex.quote(PATH_GUARD) + " " + shlex.quote(self.workspace) + " " + shlex.quote(path)
        result = self._shell(command)
        if result.exit_code == 73:
            raise PermissionDenied("symlink_or_resolved_path_denied")
        if result.exit_code != 0:
            raise SandboxError("sandbox path check failed")

    def write_text_file(self, filename: str, content: str) -> str:
        path = self.resolve(filename)
        self._guard_path(path)
        response = self._client.file.write_file(file=path, content=content, request_options=self._request_options())
        if getattr(response, "success", None) is not True:
            raise SandboxError("sandbox write failed")
        return path

    def write_bytes_file(self, filename: str, content: bytes) -> str:
        """Write arbitrary bytes (e.g. xlsx) into the sandbox via base64."""
        path = self.resolve(filename)
        self._guard_path(path)
        encoded = base64.b64encode(content).decode("ascii")
        response = self._client.file.write_file(file=path, content=encoded, encoding="base64", request_options=self._request_options())
        if getattr(response, "success", None) is not True:
            raise SandboxError("sandbox write failed")
        return path

    def read_text_file(self, filename: str) -> str:
        path = self.resolve(filename)
        self._guard_path(path)
        result = self._client.file.read_file(file=path, request_options=self._request_options())
        if getattr(result, "success", None) is False or getattr(result, "data", None) is None:
            raise SandboxError(f"read failed for '{path}': {getattr(result, 'message', '')}")
        return result.data.content

    def read_bytes_file(self, filename: str) -> bytes:
        path = self.resolve(filename)
        self._guard_path(path)
        return b"".join(self._client.file.download_file(path=path, request_options=self._request_options()))

    def delete_file(self, filename: str) -> None:
        path = self.resolve(filename)
        self._guard_path(path)
        # shlex.quote keeps the path shell-safe on the Linux sandbox.
        result = self._shell(f"rm -f -- {shlex.quote(path)}")
        if result.exit_code != 0:
            raise SandboxError("sandbox cleanup failed")

    def execute_python(
        self,
        code: str,
        timeout: Optional[int] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> ExecutionResult:
        timeout = self._settings.sandbox_default_timeout if timeout is None else timeout
        if type(timeout) is not int or not 1 <= timeout <= self._settings.sandbox_max_timeout:
            raise ValueError("timeout outside platform limits")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")
        if session_id is not None:
            raise ValueError("caller-owned sessions are not supported; use workspace files for state")
        cwd = workspace_path(self.workspace, cwd or ".", allow_root=True)
        self._guard_path(cwd)
        owned_session = "doc-" + uuid.uuid4().hex
        try:
            created = self._client.jupyter.create_session(
                session_id=owned_session, cwd=cwd, request_options=self._request_options(timeout),
            )
            data = getattr(created, "data", None)
            if getattr(created, "success", None) is not True or getattr(data, "session_id", None) != owned_session:
                raise SandboxError("sandbox session creation not confirmed")
            response = self._client.jupyter.execute_code(
                code=code, timeout=timeout, session_id=owned_session, cwd=cwd,
                request_options=self._request_options(timeout),
            )
            return self._normalize(response)
        except Exception as exc:
            raise SandboxExecutionUncertain("sandbox execution state unknown; task must stop") from exc
        finally:
            try:
                deleted = self._client.jupyter.delete_session(
                    owned_session, request_options=self._request_options(),
                )
                if getattr(deleted, "success", None) is not True:
                    raise SandboxError("session deletion not confirmed")
            except Exception as exc:
                raise SandboxExecutionUncertain("sandbox session cleanup not confirmed; task must stop") from exc

    @staticmethod
    def _normalize(resp: Any) -> ExecutionResult:
        raw = resp
        # The pinned SDK returns ResponseJupyterExecuteResponse, with execution in .data.
        if hasattr(resp, "data"):
            if getattr(resp, "success", None) is False or resp.data is None:
                return ExecutionResult(
                    status="error", error=getattr(resp, "message", None) or "missing execution data",
                    execution_uncertain=True, raw=raw,
                )
            resp = resp.data
        if getattr(resp, "status", None) not in {"ok", "error", "timeout"}:
            return ExecutionResult(status="error", error="unknown execution status", execution_uncertain=True, raw=raw)
        stdout: list[str] = []
        stderr: list[str] = []
        outputs: list[str] = []
        error: Optional[str] = None
        traceback: Optional[str] = None

        for out in resp.outputs or []:
            output_type = out.output_type
            if output_type == "stream":
                text = out.text or ""
                if out.name == "stderr":
                    stderr.append(text)
                else:
                    stdout.append(text)
            elif output_type == "error":
                if out.ename and out.evalue:
                    error = f"{out.ename}: {out.evalue}"
                else:
                    error = out.evalue or out.ename or "unknown error"
                traceback = "\n".join(out.traceback or []) or None
            elif output_type in ("execute_result", "display_data"):
                data = out.data or {}
                plain = data.get("text/plain")
                outputs.append(str(plain) if plain is not None else str(data))

        return ExecutionResult(
            status="error" if error and resp.status == "ok" else resp.status,
            stdout="".join(stdout),
            stderr="".join(stderr),
            error=error,
            traceback=traceback,
            outputs=outputs,
            raw=raw,
            execution_uncertain=resp.status == "timeout",
        )
