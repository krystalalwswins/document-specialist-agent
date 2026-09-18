"""Token estimation with usage calibration, optional tokenizer and safe fallback."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    characters: int
    method: str


class TokenEstimator:
    """Estimate prompt tokens without making tokenization a hard dependency.

    Priority is deliberately explicit:

    1. a moving chars/token ratio calibrated from real provider usage;
    2. ``tiktoken`` when it happens to be installed;
    3. a conservative mixed-language character formula.
    """

    def __init__(self, model: str, *, enable_tokenizer: bool = True) -> None:
        self._model = model
        self._encoding = self._load_encoding(model) if enable_tokenizer else None
        self._chars_per_token: float | None = None
        self._lock = RLock()

    def estimate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenEstimate:
        serialized = self.serialize(messages, tools)
        characters = len(serialized)
        with self._lock:
            calibrated = self._chars_per_token
        if calibrated is not None and calibrated > 0:
            return TokenEstimate(
                tokens=max(1, math.ceil(characters / calibrated)),
                characters=characters,
                method="calibrated_usage",
            )
        if self._encoding is not None:
            try:
                return TokenEstimate(
                    tokens=max(1, len(self._encoding.encode(serialized))),
                    characters=characters,
                    method="tokenizer",
                )
            except Exception:
                # Token estimation must remain available when an optional
                # tokenizer does not understand a provider-specific payload.
                pass

        ascii_chars = sum(1 for char in serialized if ord(char) < 128)
        non_ascii_chars = characters - ascii_chars
        # English/code is usually several characters per token, while CJK text
        # is much denser. The extra per-message allowance covers chat framing.
        tokens = math.ceil(ascii_chars / 4 + non_ascii_chars / 1.5)
        tokens += len(messages) * 4 + (len(tools or []) * 8)
        return TokenEstimate(
            tokens=max(1, tokens),
            characters=characters,
            method="character_fallback",
        )

    def observe_actual(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        prompt_tokens: int | None,
    ) -> bool:
        """Calibrate future estimates from provider-reported prompt usage."""
        if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool):
            return False
        if prompt_tokens <= 0:
            return False
        characters = len(self.serialize(messages, tools))
        if characters <= 0:
            return False
        observed = characters / prompt_tokens
        # Ignore clearly broken provider metadata instead of poisoning future calls.
        if not 0.1 <= observed <= 20:
            return False
        with self._lock:
            if self._chars_per_token is None:
                self._chars_per_token = observed
            else:
                self._chars_per_token = self._chars_per_token * 0.7 + observed * 0.3
        return True

    @staticmethod
    def serialize(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> str:
        return json.dumps(
            {"messages": messages, "tools": tools or []},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _load_encoding(model: str) -> Any | None:
        try:
            import tiktoken  # type: ignore[import-not-found]

            try:
                return tiktoken.encoding_for_model(model)
            except KeyError:
                return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None
