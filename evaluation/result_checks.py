"""Read delivered CSV bytes and compare with an independent dataset oracle."""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath


def _number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"non-finite number: {value!r}")
    return result


def validate_csv_rule(rule: dict) -> None:
    if not isinstance(rule, dict):
        raise ValueError("csv_result must be an object")
    name = rule.get("filename")
    if not isinstance(name, str) or not name or PurePosixPath(name).name != name:
        raise ValueError("csv_result.filename must be a basename")
    key = rule.get("key_column")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("csv_result.key_column is required")
    rows = rule.get("rows")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("csv_result.rows must contain expected keyed numeric rows")
    columns = None
    for group, values in rows.items():
        if not isinstance(group, str) or not group or not isinstance(values, dict) or not values:
            raise ValueError("invalid expected CSV row")
        if any(not isinstance(c, str) or not c or c == key for c in values):
            raise ValueError("invalid expected numeric column")
        if columns is not None and set(values) != columns:
            raise ValueError("all expected rows must use the same numeric columns")
        columns = set(values)
        for value in values.values():
            _number(value)
    if _number(rule.get("absolute_tolerance", "0")) < 0:
        raise ValueError("absolute_tolerance must be non-negative")


def check_csv_result(task, rule: dict, read_artifact) -> tuple[bool, str]:
    """Only read a unique artifact actually delivered by this task; fail closed."""
    try:
        validate_csv_rule(rule)
        artifacts = task.result.get("artifacts", []) if isinstance(task.result, dict) else []
        keys = [a["oss_key"] for a in artifacts
                if isinstance(a, dict) and isinstance(a.get("oss_key"), str)
                and PurePosixPath(a["oss_key"]).name == rule["filename"]]
        if len(keys) != 1:
            return False, f"expected one delivered {rule['filename']}, found {len(keys)}"
        if read_artifact is None:
            return False, "artifact reader is not configured"
        content = read_artifact(keys[0])
        if not isinstance(content, bytes) or len(content) > 2 * 1024 * 1024:
            return False, "CSV must be bytes and no larger than 2 MiB"
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
        headers = reader.fieldnames or []
        columns = {rule["key_column"], *next(iter(rule["rows"].values())).keys()}
        if len(headers) != len(set(headers)) or not columns <= set(headers):
            return False, "missing required columns or duplicate headers"
        actual = {}
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                return False, "malformed CSV row"
            group = row[rule["key_column"]]
            if group in actual:
                return False, f"duplicate group: {group}"
            actual[group] = row
        expected = rule["rows"]
        if set(actual) != set(expected):
            return False, f"group mismatch: missing={sorted(set(expected)-set(actual))}, extra={sorted(set(actual)-set(expected))}"
        tolerance = _number(rule.get("absolute_tolerance", "0"))
        for group, values in expected.items():
            for column, value in values.items():
                observed = _number(actual[group][column])
                if abs(observed - _number(value)) > tolerance:
                    return False, f"{group}.{column}: expected {value}, actual {observed}, tolerance {tolerance}"
        return True, "all expected groups and numeric values match"
    except Exception as exc:
        # Storage/encoding/parser failures are failed checks, never skipped checks.
        return False, f"CSV verification failed: {type(exc).__name__}: {exc}"
