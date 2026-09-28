"""Per-call host directories and the artifact commit protocol.

Layout for one code tool call (design note 14, §3.3)::

    <call_root>/<call_id>/control/run.py   -> /runner/run.py:ro   (never merged)
    <call_root>/<call_id>/out/             -> <task>/out:rw       (only artifact source)
    <call_root>/<call_id>/logs/            -> host-side stdout/stderr spool

`commit` implements the protocol from §3.5: scan and validate everything before
writing anything, reject symlinks and special files, enforce quotas, treat a
byte-identical target as an idempotent no-op, write each file atomically, and
report a mid-way failure instead of pretending the call succeeded.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sandbox.exec_wrapper import LOGS_DIR_NAME, OUT_DIR_NAME, RUNNER_PATH_IN_CALL

RESERVED_NAMES = frozenset({"run.py", "control", "logs", "stdout.log", "stderr.log"})
CONTROL_DIR_NAME = "control"


class CommitError(RuntimeError):
    """Artifact pre-commit validation failed; nothing was written."""


@dataclass(frozen=True)
class CallLayout:
    call_id: str
    root: Path
    control: Path
    out: Path
    logs: Path

    @property
    def runner_source_path(self) -> Path:
        return self.control / "run.py"

    @property
    def stdout_path(self) -> Path:
        return self.logs / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.logs / "stderr.log"


@dataclass
class CommitReport:
    ok: bool
    committed: list[str] = field(default_factory=list)
    skipped_identical: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def final_paths(self) -> dict[str, str]:
        return dict(self._final_paths)

    _final_paths: dict[str, str] = field(default_factory=dict, repr=False)

    def register_final_path(self, relative: str, final: str) -> None:
        self._final_paths[relative] = final


class CallWorkspace:
    def __init__(self, root: str | Path, *, allow_nested: bool = False) -> None:
        self._root = Path(root)
        self._allow_nested = allow_nested

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self, call_id: str) -> CallLayout:
        if not call_id or "/" in call_id or "\\" in call_id:
            raise ValueError("call id must be a simple name")
        root = self._root / call_id
        layout = CallLayout(
            call_id=call_id,
            root=root,
            control=root / CONTROL_DIR_NAME,
            out=root / OUT_DIR_NAME,
            logs=root / LOGS_DIR_NAME,
        )
        for directory in (layout.control, layout.out, layout.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return layout

    def discard(self, layout: CallLayout) -> None:
        shutil.rmtree(layout.root, ignore_errors=True)

    def write_runner(self, layout: CallLayout, source: str) -> Path:
        layout.runner_source_path.write_text(source, encoding="utf-8")
        return layout.runner_source_path

    # -- artifact commit ----------------------------------------------------
    def commit(
        self,
        layout: CallLayout,
        task_dir: str | Path,
        *,
        max_files: int,
        max_total_bytes: int,
    ) -> CommitReport:
        out_dir = layout.out
        task_root = Path(task_dir)
        plan = self._scan(out_dir, max_files=max_files, max_total_bytes=max_total_bytes)

        report = CommitReport(ok=True)
        to_write: list[tuple[Path, Path]] = []
        for relative, source in plan:
            target = task_root / relative
            self._ensure_inside(task_root, target)
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise CommitError("target is not a regular file: %s" % relative)
                if self._same_bytes(source, target):
                    report.skipped_identical.append(relative)
                    report.register_final_path(relative, str(target))
                    continue
                raise CommitError(
                    "refusing to overwrite existing artifact: %s" % relative
                )
            to_write.append((source, target))

        for source, target in to_write:
            relative = source.relative_to(out_dir).as_posix()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._ensure_inside(task_root, target)
                temporary = target.with_name(target.name + ".doc-agent-tmp")
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
            except Exception as exc:  # partial commit must be reported, not hidden
                report.ok = False
                report.reason = "artifact commit failed on %s: %s" % (relative, exc)
                return report
            report.committed.append(relative)
            report.register_final_path(relative, str(target))

        self._ensure_inside(task_root, task_root)
        return report

    def _scan(
        self, out_dir: Path, *, max_files: int, max_total_bytes: int
    ) -> list[tuple[str, Path]]:
        if not out_dir.exists():
            return []
        collected: list[tuple[str, Path]] = []
        total_bytes = 0
        stack: list[tuple[Path, int]] = [(out_dir, 0)]
        while stack:
            directory, depth = stack.pop()
            for entry in sorted(os.scandir(directory), key=lambda item: item.name):
                name = entry.name
                if name in RESERVED_NAMES:
                    raise CommitError("reserved internal name in artifacts: %s" % name)
                path = Path(entry.path)
                if entry.is_symlink():
                    raise CommitError("refusing to commit symlink: %s" % name)
                if entry.is_dir(follow_symlinks=False):
                    if not self._allow_nested or depth + 1 > 8:
                        raise CommitError("artifact directory nesting not allowed: %s" % name)
                    stack.append((path, depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise CommitError("refusing to commit special file: %s" % name)
                total_bytes += entry.stat(follow_symlinks=False).st_size
                if total_bytes > max_total_bytes:
                    raise CommitError("artifact commit exceeds total byte budget")
                collected.append((path.relative_to(out_dir).as_posix(), path))
                if len(collected) > max_files:
                    raise CommitError("artifact commit exceeds file count budget")
        return collected

    @staticmethod
    def _ensure_inside(root: Path, target: Path) -> None:
        root_real = os.path.realpath(str(root))
        target_real = os.path.realpath(str(target))
        if os.path.commonpath([root_real, target_real]) != root_real:
            raise CommitError("path escapes the task directory: %s" % target)

    @staticmethod
    def _same_bytes(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(65536)
                right_chunk = right_handle.read(65536)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
