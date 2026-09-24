"""Command line entry point for deterministic or explicitly enabled real evals."""

from __future__ import annotations

import argparse
from pathlib import Path

from .dataset import DEFAULT_DATASET, load_dataset
from .fake_runtime import FakeCaseExecutor
from .reporter import EvaluationReporter
from .runner import EvaluationRunner, OrchestratorCaseExecutor


DEFAULT_PROMPT_VERSION = "harness-prompts-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run versioned Document Specialist Agent evaluation cases."
    )
    parser.add_argument("--mode", choices=("fake", "real"), default="fake")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output-dir", default=".data/evaluation/reports")
    parser.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="required for real mode because it may call LLM, Sandbox and object storage",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset = load_dataset(args.dataset)
    output_dir = Path(args.output_dir)

    if args.mode == "fake":
        executor = FakeCaseExecutor(output_dir.parent / "work")
        model = "scripted-fake"
    else:
        if not args.allow_external:
            raise SystemExit(
                "real evaluation is opt-in; add --allow-external after checking "
                "LLM, Sandbox, MinIO and dataset inputs"
            )
        from agent.wiring import build_orchestrator
        from core.config import get_settings

        settings = get_settings()
        executor = OrchestratorCaseExecutor(build_orchestrator(settings))
        model = settings.llm_model

    report = EvaluationRunner(executor).run(
        dataset,
        mode=args.mode,
        model=model,
        prompt_version=args.prompt_version,
    )
    json_path, markdown_path = EvaluationReporter(output_dir).write(report)
    print(f"evaluation={'PASS' if report.passed else 'FAIL'}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    return 0 if report.passed else 1
