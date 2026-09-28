"""Host-side supervisor for one-shot execution containers (design note 14).

Trusted host process only. Responsibilities: environment preflight (startup),
per-call validation, container lifecycle, host-side timeout, forced removal with
exact-ID verification, and artifact commit. Validation never runs inside a
constructor: `preflight()` is called explicitly by application startup so that
offline tests and `build_orchestrator()` stay Docker-free.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.config import Settings
from sandbox.call_workspace import CallLayout, CallWorkspace, CommitError
from sandbox.docker_runner import CommandResult, DockerRunner, DockerTimeout
from sandbox.exec_wrapper import RUNNER_PATH_IN_CONTAINER, build_runner_source

MANAGED_LABEL = "doc-agent.managed"
TASK_LABEL = "doc-agent.task_id"
CALL_LABEL = "doc-agent.tool_call_id"
CREATED_AT_LABEL = "doc-agent.created_at"

DEFAULT_EXEC_TIMEOUT = 30.0
CLEANUP_DEADLINE_SECONDS = 20.0
VERIFY_POLL_SECONDS = 0.2
# Small margin over the requested timeout so a script that finishes right on
# time is not killed, while the host timer stays the real bound. This is NOT the
# legacy HTTP grace (10s), which would let a timeout overrun unnoticed.
EXEC_KILL_GRACE_SECONDS = 1.0


class SandboxUnavailable(RuntimeError):
    """Startup preflight failed; the sandbox capability must not be used."""


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        failed = [name for name, passed, _ in self.checks if not passed]
        if self.ok:
            return "sandbox preflight passed (%d checks)" % len(self.checks)
        return "sandbox preflight failed: %s%s" % (
            ", ".join(failed) if failed else "unknown",
            " (%s)" % self.reason if self.reason else "",
        )


@dataclass
class ExecutionOutcome:
    status: str  # ok / error / timeout / uncertain
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    execution_uncertain: bool = False
    terminal: bool = False
    output_truncated: bool = False
    container_id: str = ""
    error: str = ""
    committed: list[str] = field(default_factory=list)
    final_paths: dict[str, str] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == "ok"


class ContainerSupervisor:
    def __init__(
        self,
        runner: DockerRunner,
        settings: Settings,
        *,
        workspace: Optional[CallWorkspace] = None,
        cleanup_deadline_seconds: float = CLEANUP_DEADLINE_SECONDS,
    ) -> None:
        self._runner = runner
        self._settings = settings
        self._workspace = workspace or CallWorkspace(settings.sandbox_call_root)
        self._cleanup_deadline_seconds = cleanup_deadline_seconds

    @property
    def workspace(self) -> CallWorkspace:
        return self._workspace

    # -- shared helpers -----------------------------------------------------
    def _container_task_path(self, task_id: str) -> str:
        return "%s/tasks/%s" % (self._settings.sandbox_workspace, task_id)

    def _security_args(self) -> list[str]:
        return [
            "--network", "none",
            "--pull", "never",
            "--read-only",
            "--tmpfs", "/tmp",
            "--user", "%d:%d" % (self._settings.sandbox_uid, self._settings.sandbox_gid),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", self._settings.sandbox_cpus,
            "--memory", self._settings.sandbox_memory,
            "--memory-swap", self._settings.sandbox_memory_swap,
            "--pids-limit", str(self._settings.sandbox_pids_limit),
            "-e", "HOME=/tmp",
            "-e", "XDG_CACHE_HOME=/tmp",
            "-e", "MPLCONFIGDIR=/tmp",
        ]

    def _label_args(self, *, task_id: str, call_id: str) -> list[str]:
        return [
            "--label", "%s=1" % MANAGED_LABEL,
            "--label", "%s=%s" % (TASK_LABEL, task_id),
            "--label", "%s=%s" % (CALL_LABEL, call_id),
            "--label", "%s=%d" % (CREATED_AT_LABEL, int(time.time())),
        ]

    def _image_exists(self) -> bool:
        result = self._runner.run(
            ["image", "inspect", self._settings.sandbox_image, "--format", "{{.Id}}"],
            timeout=60.0,
        )
        return result.ok and bool(result.stdout.strip())

    def _verify_gone(self, container_id: str) -> bool:
        deadline = _deadline(self._cleanup_deadline_seconds)
        while not deadline.expired():
            inspect = self._runner.run(["inspect", container_id], timeout=30.0)
            message = (inspect.stderr or "").lower()
            if not inspect.ok and "no such object" in message:
                return True
            _sleep(VERIFY_POLL_SECONDS)
        return False

    def _destroy(self, container_id: str) -> bool:
        """Force-remove by exact ID and confirm it is gone."""
        if not container_id:
            return True
        removed = self._runner.run(["rm", "-f", container_id], timeout=120.0)
        if not removed.ok:
            # Removal can still have succeeded; verification decides.
            pass
        return self._verify_gone(container_id)

    def _spool(self, text: str, path: Path) -> tuple[str, bool]:
        limit = self._settings.sandbox_output_spool_max_bytes
        encoded = text.encode("utf-8", "replace")
        truncated = len(encoded) > limit
        if truncated:
            text = encoded[:limit].decode("utf-8", "ignore") + "\n[output truncated]"
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text, truncated

    def _cleanup(self, container_id: str, layout: Optional[CallLayout], *, keep_files: bool) -> bool:
        gone = self._destroy(container_id) if container_id else True
        if layout is not None and not keep_files:
            self._workspace.discard(layout)
        return gone

    # -- startup preflight --------------------------------------------------
    def preflight(self) -> PreflightResult:
        checks: list[tuple[str, bool, str]] = []

        def add(name: str, passed: bool, detail: str = "") -> None:
            checks.append((name, passed, detail))

        add(
            "non-root uid/gid",
            self._settings.sandbox_uid > 0 and self._settings.sandbox_gid > 0,
            "uid=%d gid=%d" % (self._settings.sandbox_uid, self._settings.sandbox_gid),
        )
        if self._settings.sandbox_uid <= 0 or self._settings.sandbox_gid <= 0:
            # Never fall back to root: refuse the capability instead.
            return PreflightResult(False, checks, reason="sandbox uid/gid must be non-root")
        add(
            "image pinned by digest",
            "@sha256:" in self._settings.sandbox_image,
            self._settings.sandbox_image,
        )
        if not self._image_exists():
            add("image present locally (no pull)", False, self._settings.sandbox_image)
            return PreflightResult(False, checks, reason="image missing; refusing to pull")
        add("image present locally (no pull)", True, self._settings.sandbox_image)

        layout = self._workspace.prepare("preflight-" + uuid.uuid4().hex[:8])
        task_dir = layout.root / "task"
        (task_dir / "out").mkdir(parents=True, exist_ok=True)
        (task_dir / "input.txt").write_text("preflight-input", encoding="utf-8")
        container_id = ""
        try:
            created = self._runner.run(
                [
                    "run", "-d",
                    "--name", "doc-agent-preflight-%s" % layout.call_id,
                    *self._label_args(task_id="preflight", call_id=layout.call_id),
                    *self._security_args(),
                    "-v", "%s:%s:ro" % ((task_dir / "out").parent.resolve(), self._container_task_path("preflight")),
                    "-v", "%s:%s/out:rw" % ((task_dir / "out").resolve(), self._container_task_path("preflight")),
                    "--entrypoint", "sleep",
                    self._settings.sandbox_image,
                    "infinity",
                ],
                timeout=120.0,
            )
            container_id = created.stdout.strip()
            add("minimal container starts", created.ok and bool(container_id), container_id[:12])
            if container_id:
                probe = self._runner.run(
                    ["exec", container_id, "sh", "-c", self._preflight_script("preflight")],
                    timeout=60.0,
                )
                parsed = _parse_fields(probe.stdout)
                for name, key, expected in (
                    ("task dir readable", "read_input", "preflight-input"),
                    ("out writable", "write_out", "ok"),
                    ("root filesystem not writable", "write_rootfs", "denied"),
                    ("tmp writable", "write_tmp", "ok"),
                    ("sibling task dirs invisible", "sibling_visible", "no"),
                ):
                    add(name, parsed.get(key) == expected, parsed.get(key, probe.stderr.strip()))
        except DockerTimeout as exc:
            add("minimal container starts", False, str(exc))
        finally:
            gone = self._cleanup(container_id, layout, keep_files=False)
            add("preflight container removed", gone, container_id[:12])

        failed = [name for name, passed, _ in checks if not passed]
        return PreflightResult(not failed, checks, reason=", ".join(failed))

    def _preflight_script(self, task_id: str) -> str:
        task_path = self._container_task_path(task_id)
        sibling = "%s/tasks/not-this-task" % self._settings.sandbox_workspace
        return "; ".join(
            [
                "printf 'read_input='; cat %s/input.txt 2>&1 || true; echo" % task_path,
                "printf 'write_out='; if printf x > %s/out/.probe 2>/dev/null; then rm -f %s/out/.probe; echo ok; else echo denied; fi" % (task_path, task_path),
                "printf 'write_rootfs='; if printf x > /.probe 2>/dev/null; then echo ok; else echo denied; fi",
                "printf 'write_tmp='; if printf x > /tmp/.probe 2>/dev/null; then echo ok; else echo denied; fi",
                "printf 'sibling_visible='; if [ -e %s ]; then echo yes; else echo no; fi" % sibling,
            ]
        )

    # -- per-call execution -------------------------------------------------
    def run(
        self,
        *,
        code: str,
        task_dir: str | Path,
        task_id: str,
        call_id: str,
        timeout: Optional[float] = None,
    ) -> ExecutionOutcome:
        script_timeout = float(timeout or self._settings.sandbox_default_timeout)
        max_timeout = float(self._settings.sandbox_max_timeout)
        if not 1.0 <= script_timeout <= max_timeout:
            raise ValueError("timeout outside platform limits")

        layout = self._workspace.prepare(call_id)
        layout_kept = False
        container_id = ""
        try:
            self._workspace.write_runner(layout, build_runner_source(code))
            # The task directory is mounted read-only, so Docker cannot create the
            # nested `out` mountpoint itself (`--read-only` makes that mkdir fail).
            # Pre-creating an empty placeholder on the host is what makes the
            # read-only-parent + writable-child layout work.
            Path(task_dir, "out").mkdir(parents=True, exist_ok=True)
            created = self._runner.run(
                [
                    "run", "-d",
                    "--name", "doc-agent-call-%s" % call_id,
                    *self._label_args(task_id=task_id, call_id=call_id),
                    *self._security_args(),
                    "-v", "%s:%s:ro" % (Path(task_dir).resolve(), self._container_task_path(task_id)),
                    "-v", "%s:%s/out:rw" % (layout.out.resolve(), self._container_task_path(task_id)),
                    "-v", "%s:%s:ro" % (layout.runner_source_path.resolve(), RUNNER_PATH_IN_CONTAINER),
                    "--entrypoint", "sleep",
                    self._settings.sandbox_image,
                    "infinity",
                ],
                timeout=120.0,
            )
            container_id = created.stdout.strip()
            if not created.ok or not container_id:
                return ExecutionOutcome(
                    status="uncertain",
                    error="container start failed: %s" % (created.stderr.strip() or "unknown"),
                    execution_uncertain=True,
                    terminal=True,
                )

            validation = self._validate_call(container_id, task_id)
            if validation is not None:
                container_id = ""
                return validation

            try:
                executed = self._runner.run(
                    [
                        "exec",
                        "-w", self._container_task_path(task_id),
                        container_id,
                        "python3", RUNNER_PATH_IN_CONTAINER,
                    ],
                    timeout=script_timeout + EXEC_KILL_GRACE_SECONDS,
                )
            except DockerTimeout:
                gone = self._destroy(container_id)
                container_id = ""
                outcome = ExecutionOutcome(
                    status="timeout",
                    error="sandbox execution exceeded %.1fs" % script_timeout,
                    execution_uncertain=True,
                    terminal=True,
                )
                if not gone:
                    outcome.error += "; container removal not confirmed"
                return outcome

            stdout, stdout_truncated = self._spool(executed.stdout, layout.stdout_path)
            stderr, stderr_truncated = self._spool(executed.stderr, layout.stderr_path)
            truncated = stdout_truncated or stderr_truncated

            gone = self._destroy(container_id)
            container_id = ""
            if not gone:
                return ExecutionOutcome(
                    status="uncertain",
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=executed.returncode,
                    output_truncated=truncated,
                    error="container removal not confirmed",
                    execution_uncertain=True,
                    terminal=True,
                )

            if not executed.ok:
                layout_kept = True  # keep logs for diagnosis; artifacts are discarded
                return ExecutionOutcome(
                    status="error",
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=executed.returncode,
                    output_truncated=truncated,
                    error=stderr.strip() or "execution failed",
                )

            try:
                report = self._workspace.commit(
                    layout,
                    task_dir,
                    max_files=self._settings.sandbox_commit_max_files,
                    max_total_bytes=self._settings.sandbox_commit_max_total_bytes,
                )
            except CommitError as exc:
                return ExecutionOutcome(
                    status="uncertain",
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=executed.returncode,
                    output_truncated=truncated,
                    error="artifact commit rejected: %s" % exc,
                    execution_uncertain=True,
                    terminal=True,
                )
            if not report.ok:
                return ExecutionOutcome(
                    status="uncertain",
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=executed.returncode,
                    output_truncated=truncated,
                    error=report.reason,
                    execution_uncertain=True,
                    terminal=True,
                    committed=list(report.committed),
                )

            layout_kept = True
            return ExecutionOutcome(
                status="ok",
                stdout=stdout,
                stderr=stderr,
                exit_code=executed.returncode,
                output_truncated=truncated,
                committed=list(report.committed),
                final_paths=report.final_paths,
            )
        finally:
            if container_id:
                self._cleanup(container_id, layout, keep_files=layout_kept)
            elif not layout_kept:
                self._workspace.discard(layout)

    def _validate_call(self, container_id: str, task_id: str) -> Optional[ExecutionOutcome]:
        """Per-call safety check; failure returns a terminal outcome."""
        try:
            probe = self._runner.run(
                ["exec", container_id, "sh", "-c", self._per_call_script(task_id)],
                timeout=60.0,
            )
        except DockerTimeout:
            self._destroy(container_id)
            return ExecutionOutcome(
                status="uncertain",
                error="per-call validation timed out",
                execution_uncertain=True,
                terminal=True,
            )
        parsed = _parse_fields(probe.stdout)
        problems = []
        if parsed.get("write_out") != "ok":
            problems.append("out/ not writable")
        if parsed.get("parent_writable") != "denied":
            problems.append("task directory is not read-only")
        if parsed.get("sibling_visible") != "no":
            problems.append("sibling task directory visible")
        if problems:
            self._destroy(container_id)
            return ExecutionOutcome(
                status="uncertain",
                error="sandbox validation failed: %s" % "; ".join(problems),
                execution_uncertain=True,
                terminal=True,
            )
        return None

    def _per_call_script(self, task_id: str) -> str:
        task_path = self._container_task_path(task_id)
        sibling = "%s/tasks/not-this-task" % self._settings.sandbox_workspace
        return "; ".join(
            [
                "printf 'write_out='; if printf x > %s/out/.probe 2>/dev/null && rm -f %s/out/.probe; then echo ok; else echo denied; fi" % (task_path, task_path),
                "printf 'parent_writable='; if printf x > %s/.probe 2>/dev/null; then echo ok; else echo denied; fi" % task_path,
                "printf 'sibling_visible='; if [ -e %s ]; then echo yes; else echo no; fi" % sibling,
            ]
        )


def _parse_fields(stdout: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


class _Deadline:
    def __init__(self, seconds: float) -> None:
        import time

        self._end = time.monotonic() + seconds
        self._time = time

    def expired(self) -> bool:
        return self._time.monotonic() >= self._end


def _deadline(seconds: float) -> _Deadline:
    return _Deadline(seconds)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
