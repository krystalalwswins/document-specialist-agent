"""Persist evaluation output as machine-readable JSON and reviewable Markdown."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .model import EvaluationReport


class EvaluationReporter:
    def __init__(self, output_dir: str | Path) -> None:
        self._output_dir = Path(output_dir)

    def write(self, report: EvaluationReport) -> tuple[Path, Path]:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stem = (
            f"{_filename_part(report.dataset_id)}-"
            f"{_filename_part(report.mode)}-{report.run_id}"
        )
        json_path = self._output_dir / f"{stem}.json"
        markdown_path = self._output_dir / f"{stem}.md"
        self._atomic_write(
            json_path,
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        self._atomic_write(markdown_path, self._markdown(report))
        return json_path, markdown_path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _markdown(report: EvaluationReport) -> str:
        metrics = report.metrics
        lines = [
            f"# Evaluation Report: {report.dataset_id}",
            "",
            f"- Run: `{report.run_id}`",
            f"- Mode / model: `{report.mode}` / `{report.model}`",
            f"- Dataset / prompt: `{report.dataset_version}` / `{report.prompt_version}`",
            f"- Result: `{'PASS' if report.passed else 'FAIL'}`",
            "",
            "## Aggregate metrics",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Case pass rate | {metrics.case_pass_rate:.2%} |",
            f"| Task completion rate | {metrics.task_completion_rate:.2%} |",
            f"| Tool selection accuracy | {metrics.tool_selection_accuracy:.2%} |",
            f"| Plan step completion rate | {metrics.plan_step_completion_rate:.2%} |",
            f"| Artifact validation pass rate | {_percent(metrics.artifact_validation_pass_rate)} |",
            f"| Average tool calls | {metrics.average_tool_calls:.2f} |",
            f"| Average replans | {metrics.average_replans:.2f} |",
            f"| Failure recovery rate | {_percent(metrics.failure_recovery_rate)} |",
            f"| Average tokens | {metrics.average_tokens:.2f} |",
            f"| P95 latency | {metrics.p95_latency_ms:.2f} ms |",
            "",
            "## Cases",
            "",
            "| Case | Category | Result | Tools | Replans | Tokens | Latency |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
        for case in report.cases:
            lines.append(
                f"| {case.case_id} | {case.category} | "
                f"{'PASS' if case.passed else 'FAIL'} | {case.tool_calls} | "
                f"{case.replans} | {case.total_tokens} | {case.latency_ms:.2f} ms |"
            )
        failures = [case for case in report.cases if not case.passed]
        if failures:
            lines.extend(["", "## Failed checks", ""])
            for case in failures:
                failed = [name for name, passed in case.checks.items() if not passed]
                lines.append(
                    f"- `{case.case_id}`: {', '.join(failed) or 'unknown'}"
                    + (f"; {case.error}" if case.error else "")
                )
        return "\n".join(lines) + "\n"


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _filename_part(value: str) -> str:
    """Keep dataset metadata from becoming a path supplied by the dataset."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return (cleaned or "evaluation")[:80]
