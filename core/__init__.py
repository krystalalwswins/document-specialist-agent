"""Cross-cutting core utilities (config now; logging later)."""

from .config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
