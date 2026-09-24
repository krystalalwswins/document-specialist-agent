"""P1-1: the model only proposes structured candidates; policy writes them."""

import json
from types import SimpleNamespace

import pytest

from memory.extractor import MemoryExtractionError, MemoryExtractor
from memory.model import MemorySourceKind, MemoryType


def _response(arguments, name="extract_memories"):
    call = SimpleNamespace(
        id="memory-call",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return self.response


def test_extractor_parses_structured_candidates_and_forces_the_tool():
    llm = FakeLLM(_response({"candidates": [{
        "memory_type": "PREFERENCE",
        "content": "Prefer concise reports",
        "confidence": 0.92,
        "source_kind": "USER_EXPLICIT",
        "evidence": "I prefer concise reports",
    }]}))

    candidates = MemoryExtractor(llm).extract(
        user_input="I prefer concise reports",
        final_answer="Understood",
        verified_evidence=[],
    )

    assert candidates[0].memory_type is MemoryType.PREFERENCE
    assert candidates[0].source_kind is MemorySourceKind.USER_EXPLICIT
    assert llm.calls[0]["tool_choice"]["function"]["name"] == "extract_memories"


def test_extractor_rejects_wrong_or_invalid_tool_envelopes():
    wrong = FakeLLM(_response({"candidates": []}, name="other"))
    invalid = FakeLLM(_response({"candidates": [{"content": "missing fields"}]}))

    with pytest.raises(MemoryExtractionError):
        MemoryExtractor(wrong).extract(
            user_input="x", final_answer="x", verified_evidence=[]
        )
    with pytest.raises(MemoryExtractionError):
        MemoryExtractor(invalid).extract(
            user_input="x", final_answer="x", verified_evidence=[]
        )
