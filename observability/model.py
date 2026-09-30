"""Versioned, JSON-only tracing contracts. No recording or runtime integration.

Events are immutable serialized snapshots at construction. Exporters must use
these snapshots, never hold references to mutable runtime messages.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_observation_id() -> str:
    return secrets.token_hex(8)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("trace payload must be finite JSON data") from exc


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")


def _id(value: str, size: int) -> None:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{size}}}", value) or int(value, 16) == 0:
        raise ValueError(f"expected a nonzero {size}-character hex ID")


@dataclass(frozen=True)
class Payload:
    """Inline, reference or explicitly unavailable data; JSON null is not missing."""
    _snapshot: str

    def __post_init__(self) -> None:
        data = json.loads(self._snapshot)
        mode = data.get("mode")
        if mode == "inline":
            if set(data) != {"mode", "value"}:
                raise ValueError("inline payload needs value")
        elif mode == "reference":
            if set(data) != {"mode", "ref", "sha256", "size_bytes"}:
                raise ValueError("invalid reference fields")
            if not isinstance(data["ref"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data["ref"]):
                raise ValueError("ref must be a logical ID, not a path or URL")
            if not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
                raise ValueError("invalid sha256")
            if type(data["size_bytes"]) is not int or data["size_bytes"] < 0:
                raise ValueError("invalid size_bytes")
        elif mode == "unavailable":
            if set(data) != {"mode", "reason"}:
                raise ValueError("invalid unavailable fields")
            _text(data["reason"], "reason")
        else:
            raise ValueError("unknown payload mode")
        _json(data)

    @classmethod
    def inline(cls, value: Any) -> Payload:
        return cls(_json({"mode": "inline", "value": value}))

    @classmethod
    def reference(cls, ref: str, sha256: str, size_bytes: int) -> Payload:
        return cls(_json(dict(mode="reference", ref=ref, sha256=sha256, size_bytes=size_bytes)))

    @classmethod
    def unavailable(cls, reason: str) -> Payload:
        return cls(_json(dict(mode="unavailable", reason=reason)))

    def to_dict(self) -> dict:
        return json.loads(self._snapshot)

    @classmethod
    def from_dict(cls, data: dict) -> Payload:
        return cls(_json(data))


@dataclass(frozen=True)
class TraceEvent:
    trace_id: str
    task_id: str
    observation_id: str
    parent_id: str | None
    sequence: int
    type: str
    name: str
    event: str
    status: str
    occurred_at: str = field(default_factory=utc_now)
    input: Payload = field(default_factory=lambda: Payload.unavailable("not_captured"))
    output: Payload = field(default_factory=lambda: Payload.unavailable("not_captured"))
    metadata: dict = field(default_factory=dict)
    error: dict | None = None
    schema_version: str = SCHEMA_VERSION
    _snapshot: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _id(self.trace_id, 32)
        _id(self.observation_id, 16)
        if self.parent_id is not None:
            _id(self.parent_id, 16)
            if self.parent_id == self.observation_id:
                raise ValueError("observation cannot parent itself")
        for name in ("task_id", "name"):
            _text(getattr(self, name), name)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported trace schema version")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if self.type not in {"span", "generation", "tool"}:
            raise ValueError("unknown observation type")
        if (self.event, self.status) not in {("started", "RUNNING"), ("ended", "SUCCESS"),
                ("ended", "ERROR"), ("ended", "CANCELLED"), ("ended", "INTERRUPTED")}:
            raise ValueError("invalid event/status combination")
        if datetime.fromisoformat(self.occurred_at).utcoffset() is None:
            raise ValueError("timestamp must include timezone")
        if not isinstance(self.metadata, dict) or (self.error is not None and not isinstance(self.error, dict)):
            raise ValueError("metadata/error must be objects")
        data = {key: getattr(self, key) for key in (
            "schema_version", "trace_id", "task_id", "observation_id", "parent_id", "sequence",
            "type", "name", "event", "status", "occurred_at", "metadata", "error")}
        data.update(input=self.input.to_dict(), output=self.output.to_dict())
        object.__setattr__(self, "_snapshot", _json(data))

    def to_dict(self) -> dict:
        return json.loads(self._snapshot)

    @classmethod
    def from_dict(cls, data: dict) -> TraceEvent:
        fields = dict(data)
        for key in ("input", "output"):
            fields[key] = Payload.from_dict(fields[key])
        return cls(**fields)


@dataclass(frozen=True)
class TraceManifest:
    trace_id: str
    task_id: str
    root_id: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _id(self.trace_id, 32)
        _id(self.root_id, 16)
        _text(self.task_id, "task_id")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported trace schema version")

    def to_dict(self) -> dict:
        return dict(schema_version=self.schema_version, trace_id=self.trace_id,
                    task_id=self.task_id, root_id=self.root_id)

    @classmethod
    def from_dict(cls, data: dict) -> TraceManifest:
        return cls(**data)
