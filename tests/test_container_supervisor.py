"""ContainerSupervisor: preflight, per-call validation, timeout and commit.

Everything here is offline: `FakeDockerRunner` stands in for the Docker CLI.
"""

import os

import pytest

from core.config import Settings
from sandbox.container_supervisor import ContainerSupervisor
from sandbox.docker_runner import FakeDockerRunner

VALIDATION_OK = "write_out=ok\nparent_writable=denied\nsibling_visible=no\n"
PREFLIGHT_OK = (
    "read_input=preflight-input\nwrite_out=ok\nwrite_rootfs=denied\n"
    "write_tmp=ok\nsibling_visible=no\n"
)


def _settings(tmp_path, **overrides) -> Settings:
    values = {"sandbox_call_root": str(tmp_path / "calls")}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _supervisor(tmp_path, runner, **overrides) -> ContainerSupervisor:
    return ContainerSupervisor(
        runner, _settings(tmp_path, **overrides), cleanup_deadline_seconds=0.4
    )


def _task_dir(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    return task_dir


def _live_containers(runner: FakeDockerRunner) -> list[str]:
    return [container.container_id for container in runner.containers.values() if not container.removed]


# --- preflight -------------------------------------------------------------


def test_preflight_passes_on_a_healthy_environment(tmp_path):
    runner = FakeDockerRunner()
    runner.queue_exec(PREFLIGHT_OK)

    result = _supervisor(tmp_path, runner).preflight()

    assert result.ok, result.summary()
    assert _live_containers(runner) == []


def test_preflight_rejects_root_identity(tmp_path):
    runner = FakeDockerRunner()
    settings = _settings(tmp_path)
    settings.sandbox_uid = 0
    supervisor = ContainerSupervisor(runner, settings, cleanup_deadline_seconds=0.4)

    result = supervisor.preflight()

    assert not result.ok
    assert any(name == "non-root uid/gid" and not passed for name, passed, _ in result.checks)
    # A misconfigured identity must not even start a container.
    assert runner.containers == {}


def test_preflight_fails_when_image_is_missing(tmp_path):
    runner = FakeDockerRunner(image_present=False)

    result = _supervisor(tmp_path, runner).preflight()

    assert not result.ok
    assert any(name == "image present locally (no pull)" and not passed for name, passed, _ in result.checks)
    assert runner.containers == {}


def test_preflight_fails_when_out_is_not_writable(tmp_path):
    runner = FakeDockerRunner()
    runner.queue_exec(PREFLIGHT_OK.replace("write_out=ok", "write_out=denied"))

    result = _supervisor(tmp_path, runner).preflight()

    assert not result.ok
    assert any(name == "out writable" and not passed for name, passed, _ in result.checks)
    assert _live_containers(runner) == []


# --- run -------------------------------------------------------------------


def test_run_commits_artifacts_and_removes_the_container(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    task_dir = _task_dir(tmp_path)
    layout = supervisor.workspace.prepare("call-1")
    (layout.out / "result.txt").write_text("artifact", encoding="utf-8")
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("done\n")

    outcome = supervisor.run(
        code="print('done')", task_dir=task_dir, task_id="task-1", call_id="call-1", timeout=5
    )

    assert outcome.status == "ok"
    assert outcome.committed == ["result.txt"]
    assert (task_dir / "out" / "result.txt").read_text(encoding="utf-8") == "artifact"
    assert outcome.execution_uncertain is False and outcome.terminal is False
    assert _live_containers(runner) == []


def test_run_returns_echoed_stdout(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("4\n")

    outcome = supervisor.run(
        code="2 + 2", task_dir=_task_dir(tmp_path), task_id="task-1", call_id="call-1", timeout=5
    )

    assert outcome.status == "ok"
    assert outcome.stdout.strip() == "4"


def test_validation_failure_blocks_user_code(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    runner.queue_exec("write_out=denied\nparent_writable=denied\nsibling_visible=no\n")

    outcome = supervisor.run(
        code="print('should not run')",
        task_dir=_task_dir(tmp_path),
        task_id="task-1",
        call_id="call-1",
        timeout=5,
    )

    assert outcome.status == "uncertain"
    assert outcome.execution_uncertain and outcome.terminal
    assert "validation failed" in outcome.error
    container = next(iter(runner.containers.values()))
    assert all("python3" not in call for call in container.exec_calls)
    assert _live_containers(runner) == []


def test_read_only_parent_violation_is_terminal(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    runner.queue_exec("write_out=ok\nparent_writable=ok\nsibling_visible=no\n")

    outcome = supervisor.run(
        code="1", task_dir=_task_dir(tmp_path), task_id="task-1", call_id="call-1", timeout=5
    )

    assert outcome.status == "uncertain" and outcome.terminal
    assert "not read-only" in outcome.error


def test_timeout_destroys_the_container_and_is_terminal(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    runner.queue_exec(VALIDATION_OK)
    runner.timeout_markers = {"python3"}

    outcome = supervisor.run(
        code="while True: pass",
        task_dir=_task_dir(tmp_path),
        task_id="task-1",
        call_id="call-1",
        timeout=2,
    )

    assert outcome.status == "timeout"
    assert outcome.execution_uncertain and outcome.terminal
    assert _live_containers(runner) == []


def test_execution_failure_is_not_terminal(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("", returncode=1, stderr="ValueError: boom")

    outcome = supervisor.run(
        code="raise ValueError('boom')",
        task_dir=_task_dir(tmp_path),
        task_id="task-1",
        call_id="call-1",
        timeout=5,
    )

    assert outcome.status == "error"
    assert outcome.terminal is False and outcome.execution_uncertain is False
    assert outcome.exit_code == 1
    assert "ValueError" in outcome.error
    assert _live_containers(runner) == []


def test_unconfirmed_removal_is_terminal(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("ok\n")
    runner.sticky_ids.add("fake-cid-1")

    outcome = supervisor.run(
        code="1", task_dir=_task_dir(tmp_path), task_id="task-1", call_id="call-1", timeout=5
    )

    assert outcome.status == "uncertain"
    assert outcome.execution_uncertain and outcome.terminal
    assert "removal not confirmed" in outcome.error


def test_commit_rejection_is_a_retryable_error(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    layout = supervisor.workspace.prepare("call-1")
    try:
        os.symlink("elsewhere", layout.out / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("ok\n")

    outcome = supervisor.run(
        code="1", task_dir=_task_dir(tmp_path), task_id="task-1", call_id="call-1", timeout=5
    )

    # Nothing was written, so this is a normal tool error the model can fix.
    assert outcome.status == "error"
    assert outcome.terminal is False and outcome.execution_uncertain is False
    assert "artifact commit rejected" in outcome.error


def test_output_over_spool_limit_is_marked_truncated(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner, sandbox_output_spool_max_bytes=1024)
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("x" * 5000 + "\n")

    outcome = supervisor.run(
        code="print('x' * 5000)",
        task_dir=_task_dir(tmp_path),
        task_id="task-1",
        call_id="call-1",
        timeout=5,
    )

    assert outcome.status == "ok"
    assert outcome.output_truncated is True
    assert "truncated" in outcome.stdout


def test_timeout_outside_platform_limits_is_rejected(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)

    with pytest.raises(ValueError):
        supervisor.run(
            code="1", task_dir=_task_dir(tmp_path), task_id="task-1", call_id="call-1", timeout=100000
        )


# --- timeout boundaries and artifact isolation -----------------------------


def _exec_timeouts(runner: FakeDockerRunner) -> list[float]:
    return [timeout for argv, timeout in runner.timeouts if argv and argv[0] == "exec" and "python3" in argv]


def test_host_deadline_is_script_timeout_plus_kill_grace(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner, sandbox_http_grace=60)
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("ok\n")

    supervisor.run(
        code="1", task_dir=_task_dir(tmp_path), task_id="task-1", call_id="call-1", timeout=5
    )

    # Trigger belongs to the host timer: 5s of user code + a small kill grace.
    # It must NOT inherit the legacy HTTP grace (60s here), which would let code
    # keep running long past the requested timeout.
    assert _exec_timeouts(runner) == [6.0]


def test_timeout_destroys_container_and_never_commits(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    task_dir = _task_dir(tmp_path)
    artifact_dir = task_dir / "out"
    artifact_dir.mkdir()
    (artifact_dir / "previous.txt").write_text("previous", encoding="utf-8")
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("partial\n")
    runner.timeout_markers = {"python3"}

    outcome = supervisor.run(
        code="while True: pass",
        task_dir=task_dir,
        task_id="task-1",
        call_id="call-1",
        timeout=2,
    )

    assert outcome.status == "timeout" and outcome.terminal
    # Removal is forced before anything could be committed.
    assert any(argv[0] == "rm" for argv in runner.calls)
    assert _live_containers(runner) == []
    assert sorted(p.name for p in artifact_dir.iterdir()) == ["previous.txt"]
    assert (artifact_dir / "previous.txt").read_text(encoding="utf-8") == "previous"


def test_failed_call_preserves_previous_artifacts(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    task_dir = _task_dir(tmp_path)
    artifact_dir = task_dir / "out"
    artifact_dir.mkdir()
    (artifact_dir / "previous.txt").write_text("previous", encoding="utf-8")
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("", returncode=1, stderr="boom")

    outcome = supervisor.run(
        code="raise ValueError('boom')",
        task_dir=task_dir,
        task_id="task-1",
        call_id="call-1",
        timeout=5,
    )

    assert outcome.status == "error"
    assert sorted(p.name for p in artifact_dir.iterdir()) == ["previous.txt"]
    assert not (tmp_path / "calls" / "call-1").exists()  # private copy discarded


def test_unconfirmed_removal_blocks_the_commit(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    task_dir = _task_dir(tmp_path)
    layout = supervisor.workspace.prepare("call-1")
    (layout.out / "new.txt").write_text("new", encoding="utf-8")
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("ok\n")
    runner.sticky_ids.add("fake-cid-1")

    outcome = supervisor.run(
        code="1", task_dir=task_dir, task_id="task-1", call_id="call-1", timeout=5
    )

    assert outcome.status == "uncertain" and outcome.terminal
    assert not (task_dir / "out" / "new.txt").exists()


def test_call_gets_a_private_copy_of_committed_artifacts(tmp_path):
    runner = FakeDockerRunner()
    supervisor = _supervisor(tmp_path, runner)
    task_dir = _task_dir(tmp_path)
    artifact_dir = task_dir / "out"
    artifact_dir.mkdir()
    (artifact_dir / "seed.txt").write_text("seed", encoding="utf-8")
    runner.queue_exec(VALIDATION_OK)
    runner.queue_exec("ok\n")

    outcome = supervisor.run(
        code="1", task_dir=task_dir, task_id="task-1", call_id="call-1", timeout=5
    )

    assert outcome.status == "ok"
    # The call ran against its own copy, so stable paths work without touching
    # previously committed artifacts.
    assert (tmp_path / "calls" / "call-1" / "out" / "seed.txt").read_text(encoding="utf-8") == "seed"
    assert (artifact_dir / "seed.txt").read_text(encoding="utf-8") == "seed"


def test_untrusted_code_is_never_auto_replayed():
    from tools.sandbox_tool import SandboxTool

    # A rejected commit is retryable by the model, but the runtime must not
    # replay the original code by itself.
    assert SandboxTool.retry_safe is False
