"""Real-Docker smoke test for the one-shot execution backend (design note 14 §5.2).

Drives the production classes (SubprocessDockerRunner + ContainerSupervisor +
TaskWorkspace) against a real Docker engine. No fakery: every check below
creates, executes in and destroys real containers.

Exit codes: 0 all checks passed, 1 a check failed, 2 NOT VERIFIED.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from core.config import Settings
from sandbox.container_supervisor import ContainerSupervisor
from sandbox.docker_runner import SubprocessDockerRunner
from sandbox.orphan_sweeper import OrphanSweeper
from sandbox.task_workspace import TaskWorkspace

CALL_TIMEOUT = 5.0
TIMEOUT_CASE_SECONDS = 2.0
DELAYED_WRITE_SECONDS = 8.0
POST_TIMEOUT_WAIT = 12.0

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    print("%-4s %-38s %s" % ("PASS" if passed else "FAIL", name, detail))


def main() -> int:
    runner = SubprocessDockerRunner()
    if not runner.docker_path:
        print("NOT VERIFIED: docker CLI is unavailable.")
        return 2

    run_id = uuid.uuid4().hex[:8]
    host_root = Path(".data") / "one-shot-smoke" / run_id / "workspace"
    call_root = Path(".data") / "one-shot-smoke" / run_id / "calls"
    settings = Settings(
        _env_file=None,
        sandbox_host_workspace=str(host_root),
        sandbox_call_root=str(call_root),
        sandbox_default_timeout=int(CALL_TIMEOUT),
        sandbox_max_timeout=120,
    )
    workspace = TaskWorkspace(
        virtual_root=settings.sandbox_workspace, host_root=host_root
    )
    supervisor = ContainerSupervisor(
        runner, settings, cleanup_deadline_seconds=10.0
    )

    print("docker        : %s" % runner.docker_path)
    print("image         : %s" % settings.sandbox_image)
    print("uid/gid       : %d:%d" % (settings.sandbox_uid, settings.sandbox_gid))
    print("run / host dir: %s / %s" % (run_id, host_root.resolve()))
    print()

    # 1. startup preflight against the real engine ---------------------------
    preflight = supervisor.preflight()
    record("preflight", preflight.ok, preflight.summary())
    for name, passed, detail in preflight.checks:
        print("       %-32s %s  %s" % (name, "ok" if passed else "FAIL", detail))
    if not preflight.ok:
        return 2

    task_id = "smoke-" + run_id
    task_dir = supervisor.workspace and workspace.task_host_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "input.txt").write_text("input-payload", encoding="utf-8")

    def call(code: str, *, timeout: float = CALL_TIMEOUT, call_id: str | None = None):
        return supervisor.run(
            code=code,
            task_dir=task_dir,
            task_id=task_id,
            call_id=call_id or ("call-" + uuid.uuid4().hex[:10]),
            timeout=timeout,
        )

    # 2. normal call + artifact commit --------------------------------------
    outcome = call(
        "from pathlib import Path\n"
        "print('hello from sandbox')\n"
        "Path('out/report.txt').write_text('artifact', encoding='utf-8')\n"
        "'done'"
    )
    record(
        "normal call succeeds",
        outcome.status == "ok",
        "status=%s exit=%s error=%r stderr=%r"
        % (outcome.status, outcome.exit_code, outcome.error, outcome.stderr[:200]),
    )
    record("stdout returned", "hello from sandbox" in outcome.stdout, outcome.stdout.strip()[:60])
    record("trailing expression echoed", "'done'" in outcome.stdout, outcome.stdout.strip()[-40:])
    record(
        "artifact committed to task dir",
        (task_dir / "out" / "report.txt").exists()
        and (task_dir / "out" / "report.txt").read_text(encoding="utf-8") == "artifact",
        str(task_dir / "out" / "report.txt"),
    )
    record("container removed after success", outcome.container_id == "", "container_id cleared")

    # 3. timeout: no delayed side effect, container gone ---------------------
    marker = task_dir / "out" / "late.txt"
    result = call(
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(%d)\n"
        "Path('out/late.txt').write_text('late', encoding='utf-8')\n" % DELAYED_WRITE_SECONDS,
        timeout=TIMEOUT_CASE_SECONDS,
    )
    record(
        "timeout is terminal and uncertain",
        result.status == "timeout" and result.execution_uncertain and result.terminal,
        "status=%s uncertain=%s terminal=%s" % (result.status, result.execution_uncertain, result.terminal),
    )
    time.sleep(POST_TIMEOUT_WAIT)
    record("no delayed side effect after timeout", not marker.exists(), str(marker))

    # 4. detached child attempt (start_new_session) -------------------------
    detached_marker = task_dir / "out" / "detached.txt"
    child = (
        "import time\nfrom pathlib import Path\n"
        "time.sleep(%d)\nPath('out/detached.txt').write_text('x', encoding='utf-8')\n"
        % DELAYED_WRITE_SECONDS
    )
    parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r], start_new_session=True)\n"
        "time.sleep(%d)\n" % (child, DELAYED_WRITE_SECONDS)
    )
    result = call(parent, timeout=TIMEOUT_CASE_SECONDS)
    record("detached-child call times out", result.status == "timeout", "status=%s" % result.status)
    time.sleep(POST_TIMEOUT_WAIT)
    record("detached child produced nothing", not detached_marker.exists(), str(detached_marker))

    # 5. concurrency --------------------------------------------------------
    results: dict[str, object] = {}

    def worker(index: int) -> None:
        results[str(index)] = call(
            "print('worker %d')\n'ok-%d'" % (index, index),
            call_id="parallel-%d-%s" % (index, uuid.uuid4().hex[:6]),
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    record(
        "concurrent calls both succeed",
        len(results) == 2 and all(item.status == "ok" for item in results.values()),
        "statuses=%s" % [item.status for item in results.values()],
    )

    # 6. orphan sweep -------------------------------------------------------
    orphan_name = "doc-agent-smoke-orphan-%s" % run_id
    created = runner.run(
        [
            "run", "-d", "--name", orphan_name,
            "--label", "doc-agent.managed=1",
            "--label", "doc-agent.task_id=dead-task",
            "--label", "doc-agent.created_at=%d" % int(time.time() - 10_000),
            "--network", "none", "--entrypoint", "sleep",
            settings.sandbox_image, "infinity",
        ],
        timeout=120.0,
    )
    orphan_id = created.stdout.strip()
    record("orphan container created for the test", bool(orphan_id), orphan_id[:12])
    sweeper = OrphanSweeper(
        runner,
        ttl_seconds=1.0,
        is_task_active=lambda _task_id: False,
        sweep_deadline_seconds=15.0,
    )
    report = sweeper.sweep()
    swept = any(orphan_id.startswith(item) or item.startswith(orphan_id) for item in report.removed)
    record(
        "orphan swept by exact id",
        swept,
        "%s (orphan=%s)" % (report.summary(), orphan_id[:12]),
    )

    # 7. leave nothing behind ----------------------------------------------
    leftovers = runner.run(
        ["ps", "-aq", "--filter", "label=doc-agent.managed=1"], timeout=60.0
    ).stdout.split()
    record("no managed containers left", leftovers == [], "left=%s" % leftovers)

    failed = [name for name, passed, _ in CHECKS if not passed]
    print()
    print("=== summary ===")
    print("checks : %d/%d passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("failed : %s" % ", ".join(failed))
        return 1
    print("=== decision ===")
    print("One-shot backend held every safety gate on real Docker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
