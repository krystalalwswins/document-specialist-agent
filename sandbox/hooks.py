"""Hermes lifecycle hooks: move data between OSS and the sandbox.

pre_execution_hook  : OSS -> sandbox (binary-safe, uniform base64 write)
post_execution_hook : sandbox -> OSS (binary read + presigned URL) + cleanup
"""

from __future__ import annotations

import logging
from typing import Optional

from core.config import Settings, get_settings
from sandbox.client import SandboxClient
from storage.storage_manager import StorageManager

logger = logging.getLogger(__name__)


class HermesHookEngine:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        sandbox: Optional[SandboxClient] = None,
        storage: Optional[StorageManager] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.sandbox = sandbox or SandboxClient(self._settings)
        self.storage = storage or StorageManager(self._settings)

    def pre_execution_hook(self, oss_key: str, sandbox_filename: str) -> str:
        """Download bytes from OSS and write them into the sandbox workspace."""
        data = self.storage.download_file_content(oss_key)
        path = self.sandbox.write_bytes_file(sandbox_filename, data)
        logger.info("pre-hook: %s -> %s (%d bytes)", oss_key, path, len(data))
        return path

    def post_execution_hook(self, sandbox_filename: str, output_oss_key: str) -> str:
        """Read the sandbox artifact, persist it to OSS, then clean the sandbox."""
        data = self.sandbox.read_bytes_file(sandbox_filename)
        url = self.storage.upload_file_content(output_oss_key, data)
        self.sandbox.delete_file(sandbox_filename)
        logger.info(
            "post-hook: %s -> %s (%d bytes)", sandbox_filename, output_oss_key, len(data)
        )
        return url

    def execute_in_sandbox(self, code: str, timeout: Optional[int] = None) -> str:
        """Execute Python in the sandbox and return normalized text output."""
        return self.sandbox.execute_python(code, timeout=timeout).text
