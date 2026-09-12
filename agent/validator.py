"""Artifact validation: a task may not report SUCCESS with a broken deliverable.

``save_report`` attaches an artifact record (object key, byte count) to its
``ToolResult``; the runtime collects those records on the task. Before a task is
allowed to succeed, each claimed artifact is re-checked against object storage
(exists, non-empty, byte count matches what was uploaded).

Only artifacts the task actually produced are checked - a task that legitimately
produces no file is still allowed to succeed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from storage.storage_manager import StorageError, StorageManager


@dataclass
class ArtifactCheck:
    oss_key: str
    ok: bool
    reason: str
    size: Optional[int] = None

    def to_event(self) -> dict[str, Any]:
        return {
            "kind": "artifact_check",
            "oss_key": self.oss_key,
            "ok": self.ok,
            "reason": self.reason,
            "size": self.size,
        }


@dataclass
class ValidationResult:
    ok: bool
    checks: list[ArtifactCheck] = field(default_factory=list)

    def events(self) -> list[dict[str, Any]]:
        return [check.to_event() for check in self.checks]

    def failure_summary(self) -> str:
        failed = [check for check in self.checks if not check.ok]
        return "; ".join(f"{check.oss_key}: {check.reason}" for check in failed)


class ArtifactValidator:
    def __init__(self, storage: StorageManager) -> None:
        self._storage = storage

    def validate(self, artifacts: Sequence[dict[str, Any]]) -> ValidationResult:
        checks: list[ArtifactCheck] = []
        for artifact in artifacts:
            key = str(artifact.get("oss_key") or "")
            expected = int(artifact.get("bytes") or 0)
            if not key:
                checks.append(ArtifactCheck("", False, "artifact record has no oss_key"))
                continue
            try:
                stat = self._storage.stat_object(key)
            except StorageError as exc:
                checks.append(ArtifactCheck(key, False, f"stat failed: {exc}"))
                continue
            if stat is None:
                checks.append(ArtifactCheck(key, False, "missing in object storage"))
            elif stat["size"] <= 0:
                checks.append(ArtifactCheck(key, False, "empty object", stat["size"]))
            elif expected and stat["size"] != expected:
                checks.append(
                    ArtifactCheck(
                        key, False, f"size mismatch: uploaded {expected}, stored {stat['size']}", stat["size"]
                    )
                )
            else:
                checks.append(ArtifactCheck(key, True, "ok", stat["size"]))
        return ValidationResult(ok=all(check.ok for check in checks), checks=checks)
