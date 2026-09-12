"""LLMClient: per-attempt timeout, bounded retry, and attempt-level observation."""

from types import SimpleNamespace

import openai
import pytest

from agent.llm_client import LLMClient
from core.config import Settings
from retry.retry_policy import classify_exception
from tools.base_tool import ErrorType


def _http():
    """Return the httpx flavour used by the installed openai SDK (httpx2 in 3.x)."""
    from openai import _base_client

    return getattr(_base_client, "httpx2", None) or getattr(_base_client, "httpx", None)


HTTP = _http()


def _request():
    return HTTP.Request("POST", "https://api.deepseek.com/chat/completions")


def _timeout_error():
    return openai.APITimeoutError(request=_request())


def _connection_error():
    return openai.APIConnectionError(request=_request())


def _rate_limit_error():
    return openai.RateLimitError("slow down", response=HTTP.Response(429, request=_request()), body=None)


def _bad_request_error():
    return openai.BadRequestError("bad request", response=HTTP.Response(400, request=_request()), body=None)


def _auth_error():
    return openai.AuthenticationError("bad key", response=HTTP.Response(401, request=_request()), body=None)


def _server_error():
    return openai.InternalServerError("upstream down", response=HTTP.Response(503, request=_request()), body=None)


class _FakeCompletions:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeOpenAI:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=_FakeCompletions(outcomes))


def _client(outcomes, **overrides):
    sleeps = []
    settings = Settings(_env_file=None, llm_api_key="test-key", **overrides)
    return LLMClient(settings=settings, client=_FakeOpenAI(outcomes), sleep=sleeps.append), sleeps


def test_retries_transient_failures_then_succeeds():
    response = SimpleNamespace(id="ok")
    client, sleeps = _client(
        [_connection_error(), _rate_limit_error(), response], llm_retry_base_delay=0
    )
    events = []

    assert client.chat([{"role": "user", "content": "hi"}], on_event=events.append) is response

    assert len(sleeps) == 2
    assert [event["final_status"] for event in events] == ["RETRYING", "RETRYING", "SUCCESS"]
    assert [event["error_type"] for event in events] == ["TRANSIENT", "TRANSIENT", None]
    assert events[-1]["retry_reason"] == "success"
    assert all(event["model"] == "deepseek-chat" for event in events)


def test_timeout_is_retried_then_raised_after_budget():
    client, sleeps = _client(
        [_timeout_error(), _timeout_error(), _timeout_error()],
        llm_max_attempts=3,
        llm_retry_base_delay=0,
    )
    events = []

    with pytest.raises(openai.APITimeoutError):
        client.chat([{"role": "user", "content": "hi"}], on_event=events.append)

    assert len(sleeps) == 2  # nothing sleeps after the final attempt
    assert [event["final_status"] for event in events] == ["RETRYING", "RETRYING", "FAILED"]
    assert events[-1]["retry_reason"] == "max_attempts_exhausted"
    assert events[-1]["error_type"] == "TIMEOUT"


def test_non_retryable_error_fails_immediately():
    client, sleeps = _client([_bad_request_error()])
    events = []

    with pytest.raises(openai.BadRequestError):
        client.chat([{"role": "user", "content": "hi"}], on_event=events.append)

    assert sleeps == []
    assert events[-1]["retry_reason"] == "non_retryable_INVALID_ARGUMENT"


def test_failing_event_sink_does_not_break_the_call():
    response = SimpleNamespace(id="ok")
    client, _ = _client([response])

    def sink(_event):
        raise RuntimeError("sink is down")

    assert client.chat([{"role": "user", "content": "hi"}], on_event=sink) is response


def test_client_is_built_with_explicit_timeout_and_no_hidden_retries(monkeypatch):
    captured = {}

    class StubOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=_FakeCompletions([SimpleNamespace(id="ok")]))

    monkeypatch.setattr("agent.llm_client.OpenAI", StubOpenAI)
    client = LLMClient(settings=Settings(_env_file=None, llm_api_key="k", llm_timeout=12.5))
    client.chat([{"role": "user", "content": "hi"}])

    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 0  # our RetryPolicy owns retries
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["api_key"] == "k"


def test_openai_errors_are_classified_for_the_retry_policy():
    assert classify_exception(_timeout_error()) is ErrorType.TIMEOUT
    assert classify_exception(_connection_error()) is ErrorType.TRANSIENT
    assert classify_exception(_rate_limit_error()) is ErrorType.TRANSIENT
    assert classify_exception(_server_error()) is ErrorType.TRANSIENT
    assert classify_exception(_auth_error()) is ErrorType.PERMISSION_DENIED
    assert classify_exception(_bad_request_error()) is ErrorType.INVALID_ARGUMENT
