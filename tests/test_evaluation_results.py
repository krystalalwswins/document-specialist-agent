"""Outcome grading must reject plausible wrong results and allow valid routes."""
from dataclasses import replace
import copy

import pytest

from evaluation.dataset import load_dataset
from evaluation.fake_runtime import FakeCaseExecutor
from evaluation.model import EvaluationExpectation
from evaluation.scorer import EvaluationScorer
from task.task_model import StepStatus


def statistics_case():
    return next(c for c in load_dataset().cases if c.id == 'structured-statistics')


def run_case(tmp_path, case):
    executor = FakeCaseExecutor(tmp_path)
    task = executor.execute(case).task
    return task, EvaluationScorer(executor.read_artifact)


def test_checks_real_delivered_bytes_not_success_claim(tmp_path):
    case = statistics_case()
    task, scorer = run_case(tmp_path, case)
    assert scorer.score_case(case, task, latency_ms=0).passed
    assert scorer.score_case(case, task, latency_ms=0).checks['csv_result']
    task.result['answer'] = 'Total is 999999'
    # This checker explicitly grades CSV, not free-text answer consistency.
    assert scorer.score_case(case, task, latency_ms=0).passed


@pytest.mark.parametrize('content,reason', [
    ('region,total,average\nEast,31,15\nWest,30,30\nALL,60,20\n', 'East.total'),
    ('region,total,average\nEast,30,15\nALL,60,20\n', 'missing'),
    ('region,total,average\nEast,30,15\nWest,30,30\nALL,60,20\nNorth,0,0\n', 'extra'),
    ('region,total,average\nEast,30,15\nEast,30,15\nWest,30,30\nALL,60,20\n', 'duplicate group'),
    ('region,total\nEast,30\nWest,30\nALL,60\n', 'missing required columns'),
    ('region,total,average\nEast,NaN,15\nWest,30,30\nALL,60,20\n', 'non-finite'),
    ('region,total,average\nEast,Infinity,15\nWest,30,30\nALL,60,20\n', 'non-finite'),
    ('region,total,average\nEast,30\n', 'malformed'),
])
def test_wrong_artifact_fails_even_when_task_succeeds(tmp_path, content, reason):
    case = statistics_case()
    fake = copy.deepcopy(case.fake)
    fake['tools'][-1]['outcomes'][0]['artifact_content'] = content
    case = replace(case, fake=fake)
    task, scorer = run_case(tmp_path, case)
    result = scorer.score_case(case, task, latency_ms=0)
    assert task.status.value == 'SUCCESS'
    assert not result.passed
    assert reason in result.check_details['csv_result']


def test_row_order_and_explicit_tolerance_are_allowed(tmp_path):
    case = statistics_case()
    fake = copy.deepcopy(case.fake)
    fake['tools'][-1]['outcomes'][0]['artifact_content'] = 'region,total,average\nALL,60,20\nWest,30,30\nEast,30.0000005,15\n'
    case = replace(case, fake=fake)
    task, scorer = run_case(tmp_path, case)
    assert scorer.score_case(case, task, latency_ms=0).passed


def test_missing_reader_object_or_delivery_fails_closed(tmp_path):
    case = statistics_case()
    task, scorer = run_case(tmp_path, case)
    assert not EvaluationScorer().score_case(case, task, latency_ms=0).passed
    assert not EvaluationScorer(lambda key: b'').score_case(case, task, latency_ms=0).passed
    def missing(key):
        raise FileNotFoundError(key)
    result = EvaluationScorer(missing).score_case(case, task, latency_ms=0)
    assert not result.passed
    assert 'FileNotFoundError' in result.check_details['csv_result']
    task.result['artifacts'].append(dict(task.result['artifacts'][0]))
    assert not scorer.score_case(case, task, latency_ms=0).passed
    task.result['artifacts'] = []
    assert not scorer.score_case(case, task, latency_ms=0).passed


def test_required_mode_allows_extra_and_reordered_successful_tools(tmp_path):
    case = statistics_case()
    task, scorer = run_case(tmp_path, case)
    task.steps.reverse()
    assert scorer.score_case(case, task, latency_ms=0).passed
    strict = replace(case, expectation=replace(case.expectation, tool_match='exact', tools=('run_python', 'save_report')))
    assert not scorer.score_case(strict, task, latency_ms=0).passed
    forbidden = replace(case, expectation=replace(case.expectation, forbidden_tools=('run_python',)))
    assert not scorer.score_case(forbidden, task, latency_ms=0).passed
    for step in task.steps:
        if step.tool == 'save_report':
            step.status = StepStatus.FAILED
    assert not scorer.score_case(case, task, latency_ms=0).checks['required_tools']


def test_optional_replan_and_explicit_prohibition(tmp_path):
    case = statistics_case()
    task, scorer = run_case(tmp_path, case)
    task.plan_events.append({'kind': 'plan_replanned'})
    assert scorer.score_case(case, task, latency_ms=0).passed
    case = replace(case, expectation=replace(case.expectation, replan=False))
    assert not scorer.score_case(case, task, latency_ms=0).passed


@pytest.mark.parametrize('change', [
    {'absolute_tolerance': '-1'}, {'absolute_tolerance': 'NaN'},
    {'filename': '../summary.csv'}, {'rows': {}},
])
def test_invalid_rules_are_rejected_when_loading(change):
    data = statistics_case().expectation.to_dict()
    data['csv_result'].update(change)
    with pytest.raises(ValueError):
        EvaluationExpectation.from_dict(data)


def test_expectation_roundtrip():
    expectation = statistics_case().expectation
    assert EvaluationExpectation.from_dict(expectation.to_dict()) == expectation


def test_failed_result_report_contains_expected_and_actual(tmp_path):
    from evaluation.reporter import EvaluationReporter
    from evaluation.runner import EvaluationRunner
    from dataclasses import replace

    case = statistics_case()
    fake = copy.deepcopy(case.fake)
    fake['tools'][-1]['outcomes'][0]['artifact_content'] = 'region,total,average\nEast,999,15\nWest,30,30\nALL,60,20\n'
    case = replace(case, fake=fake)
    dataset = replace(load_dataset(), cases=(case,))
    report = EvaluationRunner(FakeCaseExecutor(tmp_path / 'work')).run(
        dataset, mode='fake', model='scripted-fake', prompt_version='test'
    )
    assert not report.passed
    json_path, markdown_path = EvaluationReporter(tmp_path / 'reports').write(report)
    assert 'expected 30, actual 999' in json_path.read_text()
    assert 'expected 30, actual 999' in markdown_path.read_text()
