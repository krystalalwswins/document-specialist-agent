"""SandboxClient: thin, testable wrapper over agent_sandbox.Sandbox.

Facts confirmed from the installed SDK source (agent-sandbox 0.0.30):
- write_file supports binary via ``encoding="base64"`` (FileContentEncoding)
- read_file returns text only; download_file returns Iterator[bytes] for binary
- jupyter.execute_code returns ``.status`` + ``.outputs`` with output_type in
  stream / execute_result / display_data / error
- API key auth is passed via headers (X-AIO-API-Key), not a constructor kwarg
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from agent_sandbox import Sandbox

from core.config import Settings, get_settings


class SandboxError(Exception):
    """Raised when a sandbox operation fails."""


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
        if filename.startswith(self.workspace):
            return filename
        return f"{self.workspace}/{filename}"

    def write_text_file(self, filename: str, content: str) -> str:
        path = self.resolve(filename)
        self._client.file.write_file(file=path, content=content)
        return path

    def write_bytes_file(self, filename: str, content: bytes) -> str:
        """Write arbitrary bytes (e.g. xlsx) into the sandbox via base64."""
        path = self.resolve(filename)
        encoded = base64.b64encode(content).decode("ascii")
        self._client.file.write_file(file=path, content=encoded, encoding="base64")
        return path

    def read_text_file(self, filename: str) -> str:
        path = self.resolve(filename)
        result = self._client.file.read_file(file=path)
        if getattr(result, "data", None) is None:
            raise SandboxError(f"read failed for '{path}': {getattr(result, 'message', '')}")
        return result.data.content

    def read_bytes_file(self, filename: str) -> bytes:
        path = self.resolve(filename)
        return b"".join(self._client.file.download_file(path=path))

    def delete_file(self, filename: str) -> None:
        path = self.resolve(filename)
        # shlex.quote keeps the path shell-safe on the Linux sandbox.
        self._client.shell.exec_command(command=f"rm -f {shlex.quote(path)}")

    def execute_python(
        self,
        code: str,
        timeout: Optional[int] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> ExecutionResult:
        resp = self._client.jupyter.execute_code(
            code=code,
            timeout=timeout,
            session_id=session_id,
            cwd=cwd,
        )
        return self._normalize(resp)

    @staticmethod
    def _normalize(resp: Any) -> ExecutionResult:
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
            status=resp.status,
            stdout="".join(stdout),
            stderr="".join(stderr),
            error=error,
            traceback=traceback,
            outputs=outputs,
            raw=resp,
        )
