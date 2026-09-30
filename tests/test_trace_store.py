"""T1 offline acceptance assets (execution recorded separately)."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from observability.store import TraceStore, TraceCorruptError, TraceNotFound
from observability.recorder import TraceRecorder


def test_restart_payload_redaction_and_isolation(tmp_path):
    recorder = TraceRecorder(TraceStore(tmp_path, inline_bytes=20))
    tid = recorder.create("task-a", {"api_key": "secret", "text": "x" * 100})
    root = recorder.store.get(tid)["manifest"]["root_id"]
    recorder.end(tid, root, output={"answer": 42})
    store = TraceStore(tmp_path)
    data = store.for_task("task-a")
    payload = data["events"][0]["input"]
    content = store.read_payload(tid, payload["ref"])
    assert content["api_key"] == "[REDACTED]"
    other = recorder.create("task-b", {})
    with pytest.raises(TraceNotFound):
        store.read_payload(other, payload["ref"])
    assert data["capture_status"] == "COMPLETE"
    assert "secret" not in str(data) + str(content)


def test_concurrent_sequence_and_invalid_parent(tmp_path):
    r = TraceRecorder(TraceStore(tmp_path))
    tid = r.create("task", {})
    root = r.store.get(tid)["manifest"]["root_id"]
    def invoke(i):
        oid = r.start(tid, root, "tool", "read", {"i": i})
        r.end(tid, oid, output=i)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(invoke, range(10)))
    r.end(tid, root)
    data = r.store.get(tid)
    assert [e["sequence"] for e in data["events"]] == list(range(1, 23))
    assert r.start(tid, root, "tool", "late", {}) is None
    assert r.store.get(tid)["capture_status"] == "INCOMPLETE"


def test_recovery_and_corrupt_payload(tmp_path):
    r = TraceRecorder(TraceStore(tmp_path, inline_bytes=1))
    tid = r.create("task", {"data": "test"})
    assert r.store.get(tid)["open_observation_ids"]
    r.interrupt(tid)
    assert r.store.get(tid)["capture_status"] == "INCOMPLETE"
    assert r.store.get(tid)["events"][-1]["status"] == "INTERRUPTED"
    ref = r.store.get(tid)["events"][0]["input"]["ref"]
    (tmp_path / tid / "payloads" / (ref + ".json")).write_text("{}")
    with pytest.raises(TraceCorruptError):
        r.store.read_payload(tid, ref)


def test_corrupt_events_are_not_silently_accepted(tmp_path):
    r = TraceRecorder(TraceStore(tmp_path))
    tid = r.create("task", {})
    (tmp_path / tid / "events.jsonl").write_text('{"partial":')
    with pytest.raises(TraceCorruptError):
        r.store.get(tid)
    with pytest.raises(TraceNotFound):
        r.store.get("../outside")


def test_storage_failure_does_not_escape(tmp_path, monkeypatch):
    r = TraceRecorder(TraceStore(tmp_path))
    monkeypatch.setattr(r.store, "create", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert r.create("task", {}) is None
    assert r.failures == 1
