"""Offline acceptance assets for T0; no service dependencies."""
import json
import pytest
from observability import Payload, TraceEvent, TraceManifest, new_trace_id, new_observation_id


def fields():
    return dict(trace_id=new_trace_id(), task_id="task-1", observation_id=new_observation_id(),
                parent_id=None, sequence=1, type="span", name="task", event="started", status="RUNNING")


def test_retry_then_tool_and_round_trip():
    base = fields()
    root = base["observation_id"]
    manifest = TraceManifest(base["trace_id"], base["task_id"], root)
    assert TraceManifest.from_dict(manifest.to_dict()) == manifest
    events = [TraceEvent(**base)]
    for attempt, kind, status in [(1, "generation", "ERROR"), (2, "generation", "SUCCESS"), (1, "tool", "SUCCESS")]:
        item = base | dict(observation_id=new_observation_id(), parent_id=root, type=kind,
                           metadata={"attempt": attempt, "logical_call_id": "logical-1"})
        events.append(TraceEvent(**(item | dict(sequence=len(events)+1))))
        events.append(TraceEvent(**(item | dict(sequence=len(events)+1, event="ended", status=status))))
    events.append(TraceEvent(**(base | dict(sequence=len(events)+1, event="ended", status="SUCCESS"))))
    for event in events:
        assert TraceEvent.from_dict(json.loads(json.dumps(event.to_dict()))).to_dict() == event.to_dict()
    assert len({e.observation_id for e in events}) == 4


def test_capture_is_a_snapshot():
    messages = [{"content": "original"}]
    payload = Payload.inline(messages)
    meta = {"phase": "execute"}
    event = TraceEvent(**(fields() | dict(input=payload, metadata=meta)))
    messages[0]["content"] = "changed"
    meta["phase"] = "changed"
    assert event.to_dict()["input"]["value"] == [{"content": "original"}]
    assert event.to_dict()["metadata"]["phase"] == "execute"


@pytest.mark.parametrize("change", [{"sequence": 0}, {"sequence": True}, {"type": "bad"},
    {"status": "SUCCESS"}, {"event": "ended"}, {"schema_version": "99"},
    {"occurred_at": "yesterday"}, {"occurred_at": "2026-01-01T00:00:00"}])
def test_invalid_contract(change):
    with pytest.raises(ValueError):
        TraceEvent(**(fields() | change))


def test_payload_contract():
    ref = Payload.reference("payload_1", "a"*64, 12)
    assert Payload.from_dict(ref.to_dict()).to_dict() == ref.to_dict()
    assert Payload.inline(None).to_dict() != Payload.unavailable("capture_failed").to_dict()
    with pytest.raises(ValueError):
        Payload.inline(float("nan"))
    with pytest.raises(ValueError):
        Payload.reference("../secret", "a"*64, 12)
    f = fields()
    with pytest.raises(ValueError):
        TraceEvent(**(f | dict(parent_id=f["observation_id"])))
