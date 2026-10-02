"""Confirm the configured LLM endpoint answers. Never prints secrets."""

from __future__ import annotations

import sys
import time

from agent.llm_client import LLMClient
from core.config import get_settings


def main() -> int:
    settings = get_settings()
    print("base_url    :", settings.llm_base_url)
    print("model       :", settings.llm_model)
    print("api_key set :", bool(settings.llm_api_key), "(value never printed)")

    client = LLMClient(settings)
    attempts: list[dict] = []
    started = time.monotonic()
    try:
        response = client.chat(
            [{"role": "user", "content": "Reply with exactly: PONG"}],
            on_event=attempts.append,
        )
    except Exception as exc:
        print("FAIL        : %s: %s" % (type(exc).__name__, str(exc)[:200]))
        return 1
    elapsed = time.monotonic() - started

    content = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    tokens = (
        {
            "prompt": getattr(usage, "prompt_tokens", None),
            "completion": getattr(usage, "completion_tokens", None),
            "total": getattr(usage, "total_tokens", None),
        }
        if usage is not None
        else None
    )
    print("elapsed_s   : %.2f" % elapsed)
    print("reply       : %r" % content[:200])
    print("usage       :", tokens)
    print("attempts    :", len(attempts))
    return 0 if content else 1


if __name__ == "__main__":
    sys.exit(main())
