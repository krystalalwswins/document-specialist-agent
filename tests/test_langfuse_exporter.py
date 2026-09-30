"""T4 acceptance assets. No live endpoint/keys required; not executed in delivery."""
import json
from core.config import Settings
from observability.store import TraceStore
from observability.recorder import TraceRecorder
from observability.langfuse_exporter import LangfuseExporter, RetryLater, Rejected


def setup(tmp_path, transport, **options):
    config = Settings(_env_file=None, langfuse_enabled=True, langfuse_public_key="pk-fixture",
                      langfuse_secret_key="sk-fixture", **options)
    store = TraceStore(tmp_path)
    r = TraceRecorder(store, export_enabled=True)
    tid = r.create("task-fixture", {"user_input": "sum", "code_version": "commit-fixture"})
    root = store.get(tid)["manifest"]["root_id"]
    return r, tid, root, LangfuseExporter(store, config, transport=transport)


def spans(bodies):
    return [json.loads(b)["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for b in bodies]


def test_mapping_root_lifetime_and_restart_receipts(tmp_path):
    bodies = []
    r, tid, root, exp = setup(tmp_path, bodies.append)
    oid = r.start(tid, root, "generation", "llm.chat", {"messages": [], "temperature": 0},
                  metadata={"prompt_version": "local-v1", "model": "fixture-model", "attempt": 2})
    r.end(tid, oid, output={"content": "done"}, metadata={"usage": {"prompt_tokens": 10, "completion_tokens": 2,
          "total_tokens": 12, "cache_tokens": 4}, "estimated_cost_usd": 0.01})
    exp.drain_once()
    assert len(bodies) == 1  # Open root is not exported with a premature end time.
    generation = spans(bodies)[0]
    assert generation["traceId"] == tid and generation["parentSpanId"] == root
    attrs = {a["key"]: a["value"]["stringValue"] for a in generation["attributes"]}
    assert attrs["langfuse.observation.type"] == "generation"
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {"input": 10, "output": 2, "total": 12}
    assert attrs["langfuse.release"] == "commit-fixture"
    r.end(tid, root, output={"answer": "done"})
    exp.drain_once()
    assert len(bodies) == 2 and "parentSpanId" not in spans(bodies)[1]
    LangfuseExporter(r.store, exp.settings, transport=bodies.append).drain_once()
    assert len(bodies) == 2


def test_backpressure_retry_and_ambiguous_delivery(tmp_path):
    calls = []
    def limited(body):
        calls.append(body)
        raise RetryLater()
    r, tid, root, exp = setup(tmp_path, limited)
    r.end(tid, root)
    exp.drain_once()
    state = exp.state(tid)
    assert state["observations"][root]["status"] == "retry"
    exp.drain_once()
    assert len(calls) == 1  # Backoff respected.
    state["observations"][root]["next_attempt_at"] = 0
    exp._save(tid, state)
    exp.transport = lambda b: (_ for _ in ()).throw(TimeoutError())
    exp.drain_once()
    assert exp.state(tid)["observations"][root]["status"] == "uncertain"
    exp.transport = calls.append
    exp.drain_once()
    assert len(calls) == 1  # No blind resend after possible acceptance.


def test_crash_intent_and_rejection(tmp_path):
    sent = []
    r, tid, root, exp = setup(tmp_path, sent.append)
    r.end(tid, root)
    state = exp.state(tid)
    state["observations"][root] = {"status": "sending", "attempts": 1}
    exp._save(tid, state)
    exp.drain_once()
    assert not sent and exp.state(tid)["observations"][root]["status"] == "uncertain"
    state["observations"] = {}
    exp._save(tid, state)
    exp.transport = lambda b: (_ for _ in ()).throw(Rejected())
    exp.drain_once()
    assert exp.state(tid)["observations"][root]["status"] == "rejected"
    assert r.store.get(tid)["capture_status"] == "COMPLETE"


def test_redaction_large_payload_and_historical_opt_in(tmp_path):
    bodies = []
    r, tid, root, exp = setup(tmp_path, bodies.append, langfuse_payload_max_bytes=256)
    r.store.inline_bytes = 100
    oid = r.start(tid, root, "tool", "run_python", {"code": "print(80)", "email": "private@example.test",
                  "secret": "sensitive", "text": "sk-fixture"})
    r.end(tid, oid, output="x" * 1000)
    r.end(tid, root)
    historical = TraceRecorder(r.store)
    old = historical.create("older-task", {})
    historical.end(old, r.store.get(old)["manifest"]["root_id"])
    exp.drain_once()
    assert len(bodies) == 2
    exported = b"".join(bodies).decode()
    assert "private@example.test" not in exported and "sk-fixture" not in exported
    assert "sensitive" not in exported and "print(80)" in exported
    assert "export_size_limit" in exported


def test_disabled_and_changed_destination(tmp_path):
    bodies = []
    r, tid, root, exp = setup(tmp_path, bodies.append)
    r.end(tid, root)
    state = exp.state(tid)
    state["destination"] = "other-project"
    exp._save(tid, state)
    exp.drain_once()
    assert not bodies
    exp.settings.langfuse_enabled = False
    exp.drain_once()
    assert not bodies


def test_explicit_resolution(tmp_path):
    import pytest
    sent = []
    r, tid, root, exp = setup(tmp_path, lambda b: (_ for _ in ()).throw(TimeoutError()))
    r.end(tid, root)
    exp.drain_once()
    with pytest.raises(ValueError):
        exp.resolve(tid, "f" * 16, "retry")
    exp.resolve(tid, root, "retry")
    exp.transport = sent.append
    exp.drain_once()
    assert len(sent) == 1
    with pytest.raises(ValueError):
        exp.resolve(tid, root, "retry")  # Accepted observations cannot be resent.


def test_http_envelope_and_partial_rejection(tmp_path, monkeypatch):
    import base64
    import pytest
    import observability.langfuse_exporter as module
    r, tid, root, exp = setup(tmp_path, None)
    seen = []
    response_body = b'{}'
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return response_body
    class Opener:
        def open(self, request, timeout):
            seen.append((request, timeout))
            return Response()
    monkeypatch.setattr(module, "build_opener", lambda handler: Opener())
    exp._send(b'{}')
    request, timeout = seen[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert request.full_url.endswith('/api/public/otel/v1/traces')
    assert headers['authorization'] == 'Basic ' + base64.b64encode(b'pk-fixture:sk-fixture').decode()
    assert headers['x-langfuse-ingestion-version'] == '4'
    assert timeout == exp.settings.langfuse_export_timeout
    assert module.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.test') is None
    response_body = b'{"partialSuccess":{"rejectedSpans":"1"}}'
    with pytest.raises(module.UncertainDelivery):
        exp._send(b'{}')
