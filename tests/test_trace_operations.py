"""T3 acceptance assets; no live services."""
from observability import TraceStore, TraceRecorder
from observability.generation import bind_trace, reset_trace
from observability.operations import traced, snapshot
import pytest


def test_nested_operations_and_errors(tmp_path):
    store = TraceStore(tmp_path)
    r = TraceRecorder(store)
    tid = r.create("task", {})
    root = store.get(tid)["manifest"]["root_id"]
    @traced("tool.actual", kind="tool", inputs=lambda a,k: k)
    def actual(**kwargs):
        return {"ok": True}
    @traced("dispatch", inputs=lambda a,k: a[0])
    def dispatch(raw):
        return actual(path="tasks/task/" + raw["path"])
    token = bind_trace(r, tid, root)
    try:
        assert dispatch({"path": "a.txt"}) == {"ok": True}
        snapshot("plan", {"version": 1})
    finally:
        reset_trace(token)
    r.end(tid, root)
    data = store.get(tid)
    start = {e["name"]: e for e in data["events"] if e["event"] == "started"}
    assert start["tool.actual"]["parent_id"] == start["dispatch"]["observation_id"]
    assert start["tool.actual"]["input"]["value"]["path"] == "tasks/task/a.txt"
    assert data["capture_status"] == "COMPLETE"


def test_capture_failure_does_not_skip_business(tmp_path):
    r = TraceRecorder(TraceStore(tmp_path))
    tid = r.create("t", {})
    root = r.store.get(tid)["manifest"]["root_id"]
    @traced("broken_capture", inputs=lambda a,k: 1/0)
    def business():
        return 42
    @traced("failed_business")
    def fail():
        raise ValueError("business failure")
    token = bind_trace(r, tid, root)
    try:
        assert business() == 42
        with pytest.raises(ValueError, match="business failure"):
            fail()
    finally:
        reset_trace(token)
    r.end(tid, root)
    assert r.store.get(tid)["capture_status"] == "INCOMPLETE"


def test_registry_effective_parameters_and_denial(tmp_path):
    from tools.tool_registry import ToolRegistry
    from tools.base_tool import BaseTool, ToolResult
    from security.permission_manager import PermissionManager
    class Read(BaseTool):
        name = "read_file"
        description = "test"
        file_parameters = ("path",)
        def parameters_schema(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        def execute(self, **kwargs):
            return ToolResult(True, output=kwargs["path"])
    registry = ToolRegistry(PermissionManager(workspace=str(tmp_path / "sandbox")))
    registry.register(Read())
    store = TraceStore(tmp_path / "traces")
    r = TraceRecorder(store)
    tid = r.create("task1", {})
    root = store.get(tid)["manifest"]["root_id"]
    token = bind_trace(r, tid, root)
    try:
        result = registry.execute("read_file", {"path": "a.txt"}, task_id="task1")
        assert result.success
        assert not registry.execute("missing_tool", {}, task_id="task1").success
    finally:
        reset_trace(token)
    r.end(tid, root)
    events = store.get(tid)["events"]
    actual = [e for e in events if e["name"] == "tool.execute" and e["event"] == "started"]
    assert len(actual) == 1
    assert actual[0]["input"]["value"]["effective_arguments"]["path"].endswith("tasks/task1/a.txt")
    denied = [e for e in events if e["name"] == "tool.dispatch" and e["event"] == "ended"]
    assert denied[-1]["status"] == "ERROR"
