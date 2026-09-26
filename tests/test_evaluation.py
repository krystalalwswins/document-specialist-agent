"""P1-2: independent datasets, deterministic scoring and report output."""

import json

import pytest

from evaluation.cli import main
from evaluation.dataset import DEFAULT_DATASET, load_dataset
from evaluation.fake_runtime import FakeCaseExecutor
from evaluation.model import EvaluationCase, EvaluationExpectation
from evaluation.reporter import EvaluationReporter
from evaluation.runner import EvaluationRunner
from evaluation.scorer import EvaluationScorer
from task.plan_model import Plan
from task.task_manager import TaskManager


def _successful_task():
    manager = TaskManager()
    task = manager.create_task("parse a document")
    manager.start_task(task.id)
    manager.set_plan(task.id, Plan.from_dict({
        "user_input": "parse a document",
        "version": 1,
        "steps": [{
            "step_id": "parse",
            "name": "parse",
            "description": "parse the document",
            "tool": "parse_document",
            "depends_on": [],
            "completion_criteria": ["text is available"],
        }],
    }))
    step = manager.add_step(task.id, "parse_document", tool="parse_document", plan_step_id="parse")
    manager.start_step(task.id, step.id)
    manager.succeed_step(task.id, step.id, "text")
    manager.add_plan_events(task.id, [{
        "kind": "plan_step_completed",
        "step_id": "parse",
        "evidence": "text available",
    }])
    manager.add_metric_events(task.id, "llm_events", [{"total_tokens": 120}])
    manager.succeed_task(task.id, {"answer": "done"})
    return manager.get_task(task.id)


def test_default_dataset_is_versioned_and_covers_nine_required_categories():
    dataset = load_dataset(DEFAULT_DATASET)

    assert dataset.version == "1.0.0"
    assert len(dataset.cases) == 9
    assert {case.category for case in dataset.cases} == {
        "document_parsing",
        "structured_data_statistics",
        "artifact_generation",
        "missing_data",
        "tool_parameter_error",
        "transient_tool_failure",
        "large_tool_output",
        "local_replanning",
        "memory_recall",
    }


def test_scorer_uses_task_trace_instead_of_model_self_judgement():
    case = EvaluationCase(
        id="parse",
        category="document_parsing",
        description="parse",
        user_input="parse a document",
        expectation=EvaluationExpectation(
            tools=("parse_document",),
            completed_steps=("parse",),
        ),
    )

    result = EvaluationScorer().score_case(
        case, _successful_task(), latency_ms=25
    )

    assert result.passed is True
    assert result.tool_selection_accuracy == 1.0
    assert result.plan_step_completion_rate == 1.0
    assert result.total_tokens == 120


def test_fixed_fake_suite_runs_through_the_harness_and_produces_all_metrics(tmp_path):
    dataset = load_dataset()

    report = EvaluationRunner(FakeCaseExecutor(tmp_path / "work")).run(
        dataset,
        mode="fake",
        model="scripted-fake",
        prompt_version="test-prompts",
    )

    assert report.passed is True
    assert report.metrics.case_count == 9
    assert report.metrics.task_completion_rate == 1.0
    assert report.metrics.tool_selection_accuracy == 1.0
    assert report.metrics.plan_step_completion_rate == 1.0
    assert report.metrics.artifact_validation_pass_rate == 1.0
    assert report.metrics.failure_recovery_rate == 1.0
    assert report.metrics.average_tokens > 0
    assert report.metrics.p95_latency_ms >= 0


def test_reporter_writes_versioned_json_and_markdown(tmp_path):
    report = EvaluationRunner(FakeCaseExecutor(tmp_path / "work")).run(
        load_dataset(),
        mode="fake",
        model="scripted-fake",
        prompt_version="test-prompts",
    )

    json_path, markdown_path = EvaluationReporter(tmp_path / "reports").write(report)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["dataset_version"] == "1.0.0"
    assert payload["prompt_version"] == "test-prompts"
    assert payload["model"] == "scripted-fake"
    assert "Aggregate metrics" in markdown_path.read_text(encoding="utf-8")


def test_real_mode_requires_explicit_external_opt_in():
    with pytest.raises(SystemExit, match="real evaluation is opt-in"):
        main(["--mode", "real"])
