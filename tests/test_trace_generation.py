"""T2 acceptance assets: scripted client, no external calls."""
from types import SimpleNamespace
from observability import TraceStore, TraceRecorder
from observability.generation import bind_trace, reset_trace, GenerationCapture


def test_retry_attempts_and_visible_response(tmp_path):
    store = TraceStore(tmp_path)
    r = TraceRecorder(store)
    tid = r.create("task", {})
    root = store.get(tid)["manifest"]["root_id"]
    token = bind_trace(r, tid, root)
    try:
        capture = GenerationCapture({"model": "fake", "messages": [{"role": "user", "content": "hello"}]}, None, "v1")
        first = capture.start(1)
        capture.finish(first, error=RuntimeError("temporary"), duration_ms=5)
        second = capture.start(2)
        message = SimpleNamespace(content="ok", role="assistant", tool_calls=None, reasoning_content="hidden")
        response = SimpleNamespace(id="resp1", model="fake", choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])
        capture.finish(second, response=response, usage={"total_tokens": 3}, duration_ms=2)
    finally:
        reset_trace(token)
    r.end(tid, root)
    events = store.get(tid)["events"]
    generations = [e for e in events if e["type"] == "generation"]
    assert len(generations) == 4
    assert generations[0]["observation_id"] != generations[2]["observation_id"]
    assert generations[0]["metadata"]["logical_call_id"] == generations[2]["metadata"]["logical_call_id"]
    assert "hidden" not in str(events)
    assert generations[-1]["metadata"]["usage"]["total_tokens"] == 3


def test_disabled_without_context():
    capture = GenerationCapture({}, None, "v1")
    assert capture.start(1) is None
    capture.finish(None, error=RuntimeError("ignored"))


def test_llm_client_records_real_request_boundary(tmp_path):
    from agent.llm_client import LLMClient
    from core.config import Settings
    store = TraceStore(tmp_path)
    recorder = TraceRecorder(store)
    tid = recorder.create("task-client", {})
    root = store.get(tid)["manifest"]["root_id"]
    sent = []
    response = SimpleNamespace(id="r", model="fake", usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        choices=[SimpleNamespace(index=0, finish_reason="stop", message=SimpleNamespace(role="assistant", content="done", tool_calls=None))])
    def create(**kwargs):
        sent.append(kwargs)
        return response
    client = LLMClient(Settings(llm_model="fake", llm_api_key="test", trace_prompt_version="test-v1"),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    def sink(event):
        pass
    sink.trace_metadata = {"phase": "execute", "iteration": 2}
    token = bind_trace(recorder, tid, root)
    try:
        assert client.chat([{"role": "user", "content": "hello"}], on_event=sink) is response
    finally:
        reset_trace(token)
    events = store.get(tid)["events"]
    start = next(e for e in events if e["type"] == "generation" and e["event"] == "started")
    assert start["input"]["value"] == sent[0]
    assert start["metadata"]["phase"] == "execute"
    assert start["metadata"]["iteration"] == 2
    assert start["metadata"]["prompt_version"] == "test-v1"
