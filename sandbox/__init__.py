"""Sandbox integration layer (one-shot execution backend + lifecycle hooks).

The exports below are lazy (PEP 562) on purpose: importing any sandbox submodule
must not drag in the legacy HTTP client and its SDK/config dependency chain.
That client is kept only for diff testing and is no longer wired in production.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "ExecutionResult",
    "HermesHookEngine",
    "SandboxClient",
    "SandboxError",
]

_LAZY_EXPORTS = {
    "ExecutionResult": ("sandbox.client", "ExecutionResult"),
    "SandboxClient": ("sandbox.client", "SandboxClient"),
    "SandboxError": ("sandbox.client", "SandboxError"),
    "HermesHookEngine": ("sandbox.hooks", "HermesHookEngine"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError("module %r has no attribute %r" % (__name__, name)) from None
    return getattr(importlib.import_module(module_name), attribute)
