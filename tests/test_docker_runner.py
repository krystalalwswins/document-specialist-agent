"""The fake Docker CLI must stay faithful to the command shapes we emit."""

import pytest

from sandbox.docker_runner import DockerTimeout, FakeDockerRunner


def test_image_inspect_reflects_presence():
    present = FakeDockerRunner().run(["image", "inspect", "img", "--format", "{{.Id}}"], timeout=5)
    assert present.ok and present.stdout.strip()
    missing = FakeDockerRunner(image_present=False).run(["image", "inspect", "img"], timeout=5)
    assert not missing.ok


def test_run_creates_container_and_exec_consumes_queue():
    runner = FakeDockerRunner()
    created = runner.run(["run", "-d", "--label", "k=v", "img"], timeout=5)
    container_id = created.stdout.strip()
    assert container_id
    assert runner.containers[container_id].labels["k"] == "v"

    runner.queue_exec("first\n")
    runner.queue_exec("second\n")
    assert runner.run(["exec", container_id, "python3", "/runner/run.py"], timeout=5).stdout == "first\n"
    assert runner.run(["exec", container_id, "sh", "-c", "true"], timeout=5).stdout == "second\n"


def test_rm_makes_inspect_report_not_found():
    runner = FakeDockerRunner()
    container_id = runner.run(["run", "-d", "img"], timeout=5).stdout.strip()
    assert runner.run(["inspect", container_id], timeout=5).ok
    runner.run(["rm", "-f", container_id], timeout=5)
    gone = runner.run(["inspect", container_id], timeout=5)
    assert not gone.ok and "no such object" in gone.stderr.lower()


def test_sticky_container_survives_removal():
    runner = FakeDockerRunner()
    container_id = runner.run(["run", "-d", "img"], timeout=5).stdout.strip()
    runner.stick_container(container_id)
    runner.run(["rm", "-f", container_id], timeout=5)
    assert runner.run(["inspect", container_id], timeout=5).ok


def test_failed_removal_is_reported():
    runner = FakeDockerRunner()
    container_id = runner.run(["run", "-d", "img"], timeout=5).stdout.strip()
    runner.fail_removal(container_id)
    assert not runner.run(["rm", "-f", container_id], timeout=5).ok


def test_timeout_marker_raises_docker_timeout():
    runner = FakeDockerRunner()
    runner.timeout_markers = {"python3"}
    with pytest.raises(DockerTimeout):
        runner.run(["exec", "cid", "python3", "/runner/run.py"], timeout=1)


def test_ps_filters_by_label():
    runner = FakeDockerRunner()
    runner.run(["run", "-d", "--label", "doc-agent.managed=1", "img"], timeout=5)
    runner.run(["run", "-d", "--label", "other=1", "img"], timeout=5)
    listed = runner.run(["ps", "-aq", "--filter", "label=doc-agent.managed=1"], timeout=5)
    assert len(listed.stdout.split()) == 1


def test_unknown_command_is_reported():
    result = FakeDockerRunner().run(["nonsense"], timeout=5)
    assert result.returncode == 127
