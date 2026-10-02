"""Reclaim containers left behind when the host process dies.

Design note 14 §3.12. Three guards keep the sweeper from racing a live call:

1. only containers carrying the managed label are considered,
2. a container whose task is still active is never touched,
3. a container younger than the TTL is never touched,

and removal always ends with an exact-Container-ID verification instead of a
name filter.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from sandbox.container_supervisor import (
    CREATED_AT_LABEL,
    MANAGED_LABEL,
    TASK_LABEL,
)
from sandbox.docker_runner import DockerRunner

SWEEP_DEADLINE_SECONDS = 20.0
VERIFY_POLL_SECONDS = 0.2


@dataclass
class SweepReport:
    examined: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept_active: list[str] = field(default_factory=list)
    kept_young: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        return (
            "examined=%d removed=%d kept_active=%d kept_young=%d failed=%d"
            % (
                len(self.examined),
                len(self.removed),
                len(self.kept_active),
                len(self.kept_young),
                len(self.failed),
            )
        )


class OrphanSweeper:
    def __init__(
        self,
        runner: DockerRunner,
        *,
        ttl_seconds: float,
        is_task_active: Callable[[str], bool],
        clock: Callable[[], float] = time.time,
        sweep_deadline_seconds: float = SWEEP_DEADLINE_SECONDS,
    ) -> None:
        self._runner = runner
        self._ttl_seconds = float(ttl_seconds)
        self._is_task_active = is_task_active
        self._clock = clock
        self._sweep_deadline_seconds = sweep_deadline_seconds

    def sweep(self) -> SweepReport:
        report = SweepReport()
        listed = self._runner.run(
            ["ps", "-aq", "--filter", "label=%s=1" % MANAGED_LABEL], timeout=60.0
        )
        if not listed.ok:
            return report

        for container_id in listed.stdout.split():
            report.examined.append(container_id)
            labels = self._labels(container_id)
            if labels is None:
                report.failed.append(container_id)
                continue

            task_id = labels.get(TASK_LABEL, "")
            if task_id and self._task_is_active(task_id):
                report.kept_active.append(container_id)
                continue

            age = self._age_seconds(labels)
            if age is not None and age < self._ttl_seconds:
                report.kept_young.append(container_id)
                continue

            if self._remove(container_id):
                report.removed.append(container_id)
            else:
                report.failed.append(container_id)
        return report

    # -- helpers ------------------------------------------------------------
    def _task_is_active(self, task_id: str) -> bool:
        try:
            return bool(self._is_task_active(task_id))
        except Exception:
            # Never risk killing a live call because the task store misbehaved.
            return True

    def _labels(self, container_id: str) -> Optional[dict[str, str]]:
        inspected = self._runner.run(
            ["inspect", container_id, "--format", "{{json .Config.Labels}}"], timeout=30.0
        )
        if not inspected.ok:
            # Already gone is not a failure.
            return {} if "no such object" in (inspected.stderr or "").lower() else None
        try:
            parsed = json.loads(inspected.stdout.strip() or "{}")
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _age_seconds(self, labels: dict[str, str]) -> Optional[float]:
        raw = labels.get(CREATED_AT_LABEL, "")
        try:
            created = float(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, self._clock() - created)

    def _remove(self, container_id: str) -> bool:
        self._runner.run(["rm", "-f", container_id], timeout=120.0)
        # The deadline uses a monotonic clock: `self._clock` is injectable for age
        # math and may be frozen in tests, which would spin this loop forever.
        deadline = time.monotonic() + self._sweep_deadline_seconds
        while time.monotonic() < deadline:
            inspect = self._runner.run(["inspect", container_id], timeout=30.0)
            if not inspect.ok and "no such object" in (inspect.stderr or "").lower():
                return True
            time.sleep(VERIFY_POLL_SECONDS)
        return False
