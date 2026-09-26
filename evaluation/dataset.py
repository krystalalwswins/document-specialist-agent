"""Load and validate versioned JSON evaluation datasets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import EvaluationCase, EvaluationDataset


DEFAULT_DATASET = Path(__file__).with_name("datasets") / "harness_v1.json"


def load_dataset(path: str | Path = DEFAULT_DATASET) -> EvaluationDataset:
    source = Path(path)
    try:
        data: Any = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load evaluation dataset '{source}': {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("evaluation dataset root must be an object")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("evaluation dataset cases must be a list")
    return EvaluationDataset(
        id=_required_text(data, "id"),
        version=_required_text(data, "version"),
        description=_required_text(data, "description"),
        cases=tuple(EvaluationCase.from_dict(item) for item in raw_cases),
    )


def _required_text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"dataset.{field} must be a non-empty string")
    return value.strip()
