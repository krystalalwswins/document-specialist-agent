"""LLM-assisted candidate extraction; policy still owns the write decision."""

from __future__ import annotations

import json
from typing import Any, Callable

from jsonschema import Draft202012Validator

from agent.llm_client import LLMClient

from .model import MemoryCandidate, MemorySourceKind, MemoryType


EXTRACT_MEMORIES_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_memories",
        "description": "Propose only durable memories explicitly supported by supplied evidence.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidates": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "memory_type": {
                                "type": "string",
                                "enum": [item.value for item in MemoryType],
                            },
                            "content": {"type": "string", "minLength": 1},
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "source_kind": {
                                "type": "string",
                                "enum": [item.value for item in MemorySourceKind],
                            },
                            "evidence": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Exact supporting excerpt copied from the supplied source.",
                            },
                        },
                        "required": [
                            "memory_type",
                            "content",
                            "confidence",
                            "source_kind",
                            "evidence",
                        ],
                    },
                }
            },
            "required": ["candidates"],
        },
    },
}


class MemoryExtractionError(RuntimeError):
    pass


class MemoryExtractor:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._validator = Draft202012Validator(
            EXTRACT_MEMORIES_TOOL["function"]["parameters"]
        )

    def extract(
        self,
        *,
        user_input: str,
        final_answer: str,
        verified_evidence: list[str],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[MemoryCandidate]:
        messages = [
            {
                "role": "system",
                "content": (
                    "Extract at most five durable cross-task memories. Do not store guesses, "
                    "credentials, raw conversation, temporary details, or ordinary task output. "
                    "PREFERENCE and CONSTRAINT must be explicitly stated by the user. "
                    "BUSINESS_FACT may be user-explicit or verified by successful execution. "
                    "PROCEDURE must be supported by verified execution evidence. Copy evidence "
                    "verbatim from the matching supplied section. Return an empty list when no "
                    "candidate is safe and reusable."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_input": user_input[:6000],
                        "final_answer_for_context_only": final_answer[:3000],
                        "verified_execution_evidence": verified_evidence[:20],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            response = self._llm.chat(
                messages,
                tools=[EXTRACT_MEMORIES_TOOL],
                tool_choice={
                    "type": "function",
                    "function": {"name": "extract_memories"},
                },
                on_event=on_event,
            )
            calls = response.choices[0].message.tool_calls
            if len(calls or []) != 1 or calls[0].function.name != "extract_memories":
                raise ValueError("expected exactly one extract_memories call")
            arguments = json.loads(calls[0].function.arguments)
            self._validator.validate(arguments)
            return [
                MemoryCandidate(
                    memory_type=MemoryType(item["memory_type"]),
                    content=item["content"],
                    confidence=float(item["confidence"]),
                    source_kind=MemorySourceKind(item["source_kind"]),
                    evidence=item["evidence"],
                )
                for item in arguments["candidates"]
            ]
        except Exception as exc:
            raise MemoryExtractionError(
                f"memory candidate extraction failed: {type(exc).__name__}"
            ) from exc
