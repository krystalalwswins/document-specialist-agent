"""ReportTool: persist a sandbox artifact to OSS and return a presigned URL."""

from __future__ import annotations

from typing import Any

from sandbox.client import SandboxClient
from storage.storage_manager import StorageManager
from tools.base_tool import BaseTool, ToolResult


class ReportTool(BaseTool):
    name = "save_report"
    description = "Save a sandbox file to object storage and return a download URL."

    def __init__(self, client: SandboxClient, storage: StorageManager) -> None:
        self._client = client
        self._storage = storage

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sandbox_filename": {
                    "type": "string",
                    "description": "file to read from the sandbox workspace",
                },
                "oss_key": {
                    "type": "string",
                    "description": "destination object key, e.g. reports/result.csv",
                },
            },
            "required": ["sandbox_filename", "oss_key"],
        }

    def execute(self, sandbox_filename: str, oss_key: str) -> ToolResult:
        try:
            data = self._client.read_bytes_file(sandbox_filename)
            url = self._storage.upload_file_content(oss_key, data)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, output=url)
