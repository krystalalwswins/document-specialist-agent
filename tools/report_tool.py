"""ReportTool: persist a sandbox artifact to OSS and return a presigned URL."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sandbox.client import SandboxClient
if TYPE_CHECKING:
    from storage.storage_manager import StorageManager
from tools.base_tool import BaseTool, ToolResult
from security.permission_manager import object_key
from agent.runtime import current_run
from documents.files import artifact_metadata


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
        context = current_run.get()
        prefix = context.report_prefix if context and context.report_prefix else self._report_prefix
        object_key(prefix, oss_key)
        data = self._client.read_bytes_file(sandbox_filename)
        metadata = artifact_metadata(sandbox_filename, oss_key, data, context.requirements if context else {})
        url = self._storage.upload_file_content(oss_key, data)
        metadata['url'] = url
        metadata['expires_in_seconds'] = 3600
        return ToolResult(success=True, output=url, artifact=metadata)
