"""Deprecated compatibility shim.

Use `from sandbox.hooks import HermesHookEngine` in new code. Kept so the
existing agent_core.py keeps working until the agent module migration.
"""

from sandbox.hooks import HermesHookEngine

__all__ = ["HermesHookEngine"]
