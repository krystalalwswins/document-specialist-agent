"""P1-3 assets: aggregation, price estimates and cumulative budgets."""

from decimal import Decimal

import pytest

from context.manager import ContextManager, ContextSnapshot
from context.token_estimator import TokenEstimate
from metering.budget import (
    BudgetController,
    TokenBudgetExceededError,
    TokenBudgetPolicy,
)
from metering.meter import UsageMeter
from metering.pricing import ModelPrice, PricingCatalog
from task.task_manager import TaskManager


def _running_task(manager: TaskManager):
    task = manager.create_task("measure this task")
    manager.start_task(task.id)
    return task


def _meter(manager: TaskManager) -> UsageMeter:
    return UsageMeter(
        manager,
        PricingCatalog(
            "test-prices-2026-09-24",
            {
                "model-a": ModelPrice(
                    input_usd_per_million=Decimal("1"),
                    cached_input_usd_per_million=Decimal("0.5"),
                    output_usd_per_million=Decimal("4"),
                )
            },
        ),
    )


def test_usage_is_aggregated_by_task_phase_and_iteration_with_cache_pricing():
    manager = TaskManager()
    task = _running_task(manager)
    meter = _meter(manager)

    meter.event_sink(task.id, phase="plan")({
        "model": "model-a",
        "final_status": "SUCCESS",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "cache_tokens": 0,
        "duration_ms": 12,
    })
    execute_sink = meter.event_sink(task.id, phase="execute", iteration=1)
    execute_sink({
        "model": "model-a",
        "final_status": "RETRYING",
        "duration_ms": 5,
    })
    execute_sink({
        "model": "model-a",
        "final_status": "SUCCESS",
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
        "cache_tokens": 250,
        "duration_ms": 30,
    })

    usage = manager.get_task(task.id).metrics["usage"]
    assert usage["pricing_version"] == "test-prices-2026-09-24"
    assert usage["cost_kind"] == "estimate_not_provider_bill"
    assert usage["task"]["total_tokens"] == 1620
    assert usage["task"]["cache_tokens"] == 250
    assert usage["task"]["attempts"] == 3
    assert usage["task"]["failed_attempts"] == 1
    assert usage["by_phase"]["plan"]["total_tokens"] == 120
    assert usage["by_iteration"]["execute:1"]["total_tokens"] == 1500
    assert usage["by_iteration"]["execute:1"]["estimated_cost_usd"] == 0.002875


def test_unknown_model_keeps_tokens_but_marks_cost_unavailable():
    manager = TaskManager()
    task = _running_task(manager)
    meter = _meter(manager)

    meter.event_sink(task.id, phase="execute", iteration=1)({
        "model": "unpriced-model",
        "final_status": "SUCCESS",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    })

    usage = manager.get_task(task.id).metrics["usage"]
    assert usage["task"]["total_tokens"] == 15
    assert usage["task"]["estimated_cost_usd"] is None
    assert usage["unpriced_models"] == ["unpriced-model"]


def test_soft_budget_requests_convergence_and_hard_budget_raises_once():
    manager = TaskManager()
    task = _running_task(manager)
    meter = _meter(manager)
    controller = BudgetController(
        manager, meter, TokenBudgetPolicy(soft_tokens=50, hard_tokens=100)
    )

    sink = meter.event_sink(task.id, phase="plan")
    sink({
        "model": "model-a",
        "final_status": "SUCCESS",
        "prompt_tokens": 50,
        "completion_tokens": 10,
        "total_tokens": 60,
    })
    assert controller.before_call(task.id, phase="execute", iteration=1) is True
    assert controller.before_call(task.id, phase="execute", iteration=1) is True

    sink({
        "model": "model-a",
        "final_status": "SUCCESS",
        "prompt_tokens": 30,
        "completion_tokens": 10,
        "total_tokens": 40,
    })
    with pytest.raises(TokenBudgetExceededError, match="hard_budget_exceeded"):
        controller.after_call(task.id, phase="execute", iteration=1)

    kinds = [event["kind"] for event in manager.get_task(task.id).metrics["budget_events"]]
    assert kinds == ["token_soft_budget_reached", "token_hard_budget_exceeded"]


def test_memory_capture_is_measured_but_not_charged_to_agent_loop_budget():
    manager = TaskManager()
    task = _running_task(manager)
    meter = _meter(manager)

    meter.event_sink(task.id, phase="memory_capture")({
        "model": "model-a",
        "final_status": "SUCCESS",
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
    })

    usage = manager.get_task(task.id).metrics["usage"]
    assert usage["task"]["total_tokens"] == 100
    assert usage["by_phase"]["memory_capture"]["total_tokens"] == 100
    assert usage["budgeted_total_tokens"] == 0


def test_soft_budget_signal_forces_context_manager_convergence_path():
    class FixedEstimator:
        def estimate(self, messages, tools):
            return TokenEstimate(tokens=10, characters=40, method="test")

    manager = ContextManager(
        FixedEstimator(),
        object(),  # no complete old group exists, so compactor is not invoked
        target_tokens=5,
        soft_limit=20,
        hard_limit=30,
        recent_groups=1,
        summary_max_chars=500,
    )
    events = []
    messages = [{"role": "system", "content": "keep"}]

    prepared = manager.prepare(
        messages,
        [],
        manager.new_session(pinned_count=1),
        ContextSnapshot("goal", "plan", [], ["step"]),
        force_compaction=True,
        on_context_event=events.append,
    )

    assert prepared == messages
    assert [event["kind"] for event in events] == [
        "context_estimated",
        "context_budget_forced",
        "context_final_size",
    ]
    assert events[-1]["action"] == "recent_groups_preserved"
