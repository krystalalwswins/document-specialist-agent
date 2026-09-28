"""OrphanSweeper must reclaim dead containers without racing a live call."""

from sandbox.container_supervisor import CREATED_AT_LABEL, MANAGED_LABEL, TASK_LABEL
from sandbox.docker_runner import FakeDockerRunner
from sandbox.orphan_sweeper import OrphanSweeper


NOW = 1_000_000.0


def _managed(runner: FakeDockerRunner, *, task_id: str, age_seconds: float) -> str:
    created = runner.run(
        [
            "run", "-d",
            "--label", "%s=1" % MANAGED_LABEL,
            "--label", "%s=%s" % (TASK_LABEL, task_id),
            "--label", "%s=%d" % (CREATED_AT_LABEL, int(NOW - age_seconds)),
            "img",
        ],
        timeout=5,
    )
    return created.stdout.strip()


def _sweeper(runner: FakeDockerRunner, *, active=(), ttl=300.0) -> OrphanSweeper:
    return OrphanSweeper(
        runner,
        ttl_seconds=ttl,
        is_task_active=lambda task_id: task_id in active,
        clock=lambda: NOW,
        sweep_deadline_seconds=0.4,
    )


def test_removes_old_container_of_an_inactive_task():
    runner = FakeDockerRunner()
    container_id = _managed(runner, task_id="task-1", age_seconds=10_000)

    report = _sweeper(runner).sweep()

    assert report.removed == [container_id]
    assert report.ok
    assert runner.run(["inspect", container_id], timeout=5).returncode != 0


def test_keeps_container_whose_task_is_still_active():
    runner = FakeDockerRunner()
    container_id = _managed(runner, task_id="task-1", age_seconds=10_000)

    report = _sweeper(runner, active={"task-1"}).sweep()

    assert report.kept_active == [container_id]
    assert report.removed == []
    assert runner.run(["inspect", container_id], timeout=5).ok


def test_keeps_a_container_younger_than_the_ttl():
    runner = FakeDockerRunner()
    container_id = _managed(runner, task_id="task-1", age_seconds=5)

    report = _sweeper(runner, ttl=300.0).sweep()

    assert report.kept_young == [container_id]
    assert runner.run(["inspect", container_id], timeout=5).ok


def test_removes_container_without_created_at_when_task_is_inactive():
    runner = FakeDockerRunner()
    created = runner.run(
        ["run", "-d", "--label", "%s=1" % MANAGED_LABEL, "--label", "%s=task-9" % TASK_LABEL, "img"],
        timeout=5,
    )
    container_id = created.stdout.strip()

    report = _sweeper(runner).sweep()

    assert report.removed == [container_id]


def test_ignores_containers_without_the_managed_label():
    runner = FakeDockerRunner()
    other = runner.run(["run", "-d", "--label", "other=1", "img"], timeout=5)

    report = _sweeper(runner).sweep()

    assert report.examined == []
    assert runner.run(["inspect", other.stdout.strip()], timeout=5).ok


def test_failed_removal_is_reported_as_failure():
    runner = FakeDockerRunner()
    container_id = _managed(runner, task_id="task-1", age_seconds=10_000)
    runner.stick_container(container_id)

    report = _sweeper(runner).sweep()

    assert report.removed == []
    assert report.failed == [container_id]
    assert report.ok is False


def test_active_task_lookup_failure_keeps_the_container():
    runner = FakeDockerRunner()
    container_id = _managed(runner, task_id="task-1", age_seconds=10_000)

    def explode(_task_id):
        raise RuntimeError("task store unavailable")

    report = OrphanSweeper(
        runner, ttl_seconds=1.0, is_task_active=explode, clock=lambda: NOW, sweep_deadline_seconds=0.4
    ).sweep()

    assert report.kept_active == [container_id]
    assert runner.run(["inspect", container_id], timeout=5).ok
