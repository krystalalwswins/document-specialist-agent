"""ReportTool: persist a sandbox artifact to OSS and return a presigned URL."""

from __future__ import annotations

import mimetypes
from typing import Any

from sandbox.client import SandboxClient
from storage.storage_manager import StorageManager
from tools.base_tool import BaseTool, ToolResult
from security.permission_manager import object_key


class ReportTool(BaseTool):
    name = "save_report"
    required_permissions = frozenset({"file.read", "artifact.write"})
    file_parameters = ("sandbox_filename",)
    object_key_parameters = ("oss_key",)
    description = "Save a sandbox file to object storage and return a download URL."

    def __init__(self, client: SandboxClient, storage: StorageManager, report_prefix: str = "reports") -> None:
        self._client = client
        self._storage = storage
        self._report_prefix = report_prefix

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
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
        object_key(self._report_prefix, oss_key)
        data = self._client.read_bytes_file(sandbox_filename)
        url = self._storage.upload_file_content(oss_key, data)
        return ToolResult(
            success=True,
            output=url,
            metadata={
                "oss_key": oss_key,
                "bytes": len(data),
                "content_type": mimetypes.guess_type(oss_key)[0] or "application/octet-stream",
                "sandbox_filename": sandbox_filename,
            },
        )
