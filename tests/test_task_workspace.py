"""Host task workspace mapping and the one-shot client adapter."""

import pytest

from core.config import Settings
from sandbox.container_supervisor import ExecutionOutcome
from sandbox.one_shot_client import OneShotSandboxClient
from sandbox.task_workspace import TaskWorkspace
from security.permission_manager import PermissionDenied

TASK_DIR = "/home/gem/workspace/tasks/task-1"


def _workspace(tmp_path) -> TaskWorkspace:
    return TaskWorkspace(virtual_root="/home/gem/workspace", host_root=tmp_path / "ws")


def test_task_directory_mapping(tmp_path):
    workspace = _workspace(tmp_path)
    assert workspace.task_virtual_dir("task-1") == TASK_DIR
    assert workspace.task_host_dir("task-1") == tmp_path / "ws" / "tasks" / "task-1"


def test_ensure_task_dir_creates_the_host_directory(tmp_path):
    directory = _workspace(tmp_path).ensure_task_dir("task-1")
    assert directory.is_dir()


def test_write_and_read_round_trip(tmp_path):
    workspace = _workspace(tmp_path)
    path = workspace.write_text(TASK_DIR + "/report.txt", "data")
    assert path.read_text(encoding="utf-8") == "data"
    assert workspace.read_text(TASK_DIR + "/report.txt") == "data"
    assert workspace.exists(TASK_DIR + "/report.txt")


def test_write_creates_parent_directories(tmp_path):
    workspace = _workspace(tmp_path)
    workspace.write_bytes(TASK_DIR + "/out/result.bin", b"\x00\x01")
    assert (tmp_path / "ws" / "tasks" / "task-1" / "out" / "result.bin").read_bytes() == b"\x00\x01"


def test_traversal_is_rejected(tmp_path):
    workspace = _workspace(tmp_path)
    with pytest.raises(PermissionDenied):
        workspace.read_text("/home/gem/workspace/tasks/../../etc/passwd")


def test_invalid_task_id_is_rejected(tmp_path):
    with pytest.raises(PermissionDenied):
        _workspace(tmp_path).task_host_dir("../evil")


def test_delete_removes_the_file(tmp_path):
    workspace = _workspace(tmp_path)
    workspace.write_text(TASK_DIR + "/gone.txt", "x")
    workspace.delete(TASK_DIR + "/gone.txt")
    assert not workspace.exists(TASK_DIR + "/gone.txt")


def test_no_temporary_file_is_left_behind(tmp_path):
    workspace = _workspace(tmp_path)
    workspace.write_text(TASK_DIR + "/report.txt", "data")
    leftovers = list((tmp_path / "ws" / "tasks" / "task-1").glob("*.doc-agent-tmp"))
    assert leftovers == []


class FakeSupervisor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


def _client(tmp_path, outcome):
    supervisor = FakeSupervisor(outcome)
    client = OneShotSandboxClient(
        Settings(_env_file=None), supervisor, _workspace(tmp_path)
    )
    return client, supervisor


def test_execute_python_derives_task_id_from_cwd(tmp_path):
    outcome = ExecutionOutcome(status="ok", stdout="4\n")
    client, supervisor = _client(tmp_path, outcome)

    result = client.execute_python("2 + 2", cwd=TASK_DIR)

    assert result.status == "ok"
    assert result.text.strip() == "4"
    call = supervisor.calls[0]
    assert call["task_id"] == "task-1"
    assert call["task_dir"] == tmp_path / "ws" / "tasks" / "task-1"
    assert call["call_id"].startswith("call-")


def test_execute_python_requires_a_task_directory(tmp_path):
    client, _ = _client(tmp_path, ExecutionOutcome(status="ok"))
    with pytest.raises(PermissionDenied):
        client.execute_python("1", cwd="/tmp")


def test_execute_python_rejects_caller_owned_sessions(tmp_path):
    client, _ = _client(tmp_path, ExecutionOutcome(status="ok"))
    with pytest.raises(ValueError):
        client.execute_python("1", cwd=TASK_DIR, session_id="mine")


def test_uncertain_outcome_is_visible_to_the_tool_layer(tmp_path):
    outcome = ExecutionOutcome(
        status="timeout", error="too slow", execution_uncertain=True, terminal=True
    )
    client, _ = _client(tmp_path, outcome)

    result = client.execute_python("while True: pass", cwd=TASK_DIR, timeout=2)

    assert result.execution_uncertain is True
    from tools.sandbox_tool import execution_to_tool_result

    tool_result = execution_to_tool_result(result)
    assert tool_result.success is False
    assert tool_result.terminal is True


def test_file_helpers_accept_relative_names(tmp_path):
    client, _ = _client(tmp_path, ExecutionOutcome(status="ok"))
    client.write_text_file("tasks/task-1/note.txt", "hello")
    assert client.read_text_file("tasks/task-1/note.txt") == "hello"
