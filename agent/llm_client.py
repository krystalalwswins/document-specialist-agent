"""LLMClient: thin wrapper over the OpenAI-compatible client.

Centralizes model / base_url / api_key wiring and is injectable so the
planner and executor can be unit-tested with a fake LLM.
"""

from __future__ import annotations

from typing import Any, Optional

from openai import OpenAI

from core.config import Settings, get_settings


class LLMClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        client: Optional[Any] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or OpenAI(
            api_key=self._settings.llm_api_key,
            base_url=self._settings.llm_base_url,
        )

    @property
    def model(self) -> str:
        return self._settings.llm_model

    def chat(self, messages: list[dict[str, Any]], tools=None, tool_choice=None) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self._client.chat.completions.create(**kwargs)
