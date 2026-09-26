"""Aggregate append-only LLM events into task, phase and iteration usage."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable

from task.task_manager import TaskManager

from .pricing import PricingCatalog


# Post-success memory extraction is still measured, but it is best-effort work
# after the business task reaches SUCCESS and therefore does not consume this
# Agent Loop budget.
BUDGETED_PHASES = frozenset({"plan", "replan", "context_compaction", "execute"})


def _token(event: dict[str, Any], key: str) -> int:
    value = event.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _cost_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000000000001")))


@dataclass
class _Totals:
    attempts: int = 0
    successful_responses: int = 0
    failed_attempts: int = 0
    usage_missing_responses: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
    duration_ms: int = 0
    estimated_cost: Decimal = Decimal("0")
    cost_complete: bool = True

    def add(self, event: dict[str, Any], catalog: PricingCatalog) -> None:
        self.attempts += 1
        self.duration_ms += _token(event, "duration_ms")
        if event.get("final_status") != "SUCCESS":
            self.failed_attempts += 1
            return

        self.successful_responses += 1
        has_usage = any(
            event.get(key) is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_tokens")
        )
        if not has_usage:
            self.usage_missing_responses += 1
            self.cost_complete = False
            return

        prompt = _token(event, "prompt_tokens")
        completion = _token(event, "completion_tokens")
        total = _token(event, "total_tokens") or prompt + completion
        cached = min(_token(event, "cache_tokens"), prompt)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        self.cache_tokens += cached

        price = catalog.price_for(str(event.get("model", "")))
        if price is None:
            self.cost_complete = False
            return
        self.estimated_cost += price.estimate(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_tokens=cached,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successful_responses": self.successful_responses,
            "failed_attempts": self.failed_attempts,
            "usage_missing_responses": self.usage_missing_responses,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_tokens": self.cache_tokens,
            "duration_ms": self.duration_ms,
            "estimated_cost_usd": (
                _cost_number(self.estimated_cost) if self.cost_complete else None
            ),
        }


class UsageMeter:
    """Persist raw model events and a derived, replaceable usage snapshot."""

    def __init__(self, task_manager: TaskManager, pricing: PricingCatalog) -> None:
        self._task_manager = task_manager
        self._pricing = pricing

    def event_sink(
        self, task_id: str, *, phase: str, iteration: int | None = None
    ) -> Callable[[dict[str, Any]], None]:
        context: dict[str, Any] = {"phase": phase}
        if iteration is not None:
            context["iteration"] = iteration

        def sink(event: dict[str, Any]) -> None:
            self._task_manager.add_metric_events(
                task_id, "llm_events", [{**event, **context}]
            )
            self.refresh(task_id)

        return sink

    def refresh(self, task_id: str) -> dict[str, Any]:
        task = self._task_manager.get_task(task_id)
        snapshot = self.aggregate(task.metrics.get("llm_events", []))
        self._task_manager.set_metric(task_id, "usage", snapshot)
        return snapshot

    def snapshot(self, task_id: str) -> dict[str, Any]:
        task = self._task_manager.get_task(task_id)
        stored = task.metrics.get("usage")
        if isinstance(stored, dict):
            return stored
        return self.refresh(task_id)

    def aggregate(self, events: Iterable[dict[str, Any]]) -> dict[str, Any]:
        records = [event for event in events if isinstance(event, dict)]
        task_totals = self._totals(records)
        phases = sorted({str(event.get("phase", "unknown")) for event in records})
        iterations = sorted({
            (str(event.get("phase", "unknown")), int(event["iteration"]))
            for event in records
            if isinstance(event.get("iteration"), int)
        })
        models = sorted({
            str(event["model"]) for event in records if event.get("model")
        })
        unpriced_models = sorted({
            str(event["model"])
            for event in records
            if event.get("final_status") == "SUCCESS"
            and event.get("model")
            and self._pricing.price_for(str(event["model"])) is None
        })
        budgeted_total_tokens = sum(
            _token(event, "total_tokens")
            or (_token(event, "prompt_tokens") + _token(event, "completion_tokens"))
            for event in records
            if event.get("final_status") == "SUCCESS"
            and event.get("phase") in BUDGETED_PHASES
        )
        return {
            "cost_kind": "estimate_not_provider_bill",
            "pricing_version": self._pricing.version,
            "currency": self._pricing.currency,
            "models": models,
            "unpriced_models": unpriced_models,
            "budgeted_total_tokens": budgeted_total_tokens,
            "task": task_totals.to_dict(),
            "by_phase": {
                phase: self._totals(
                    event for event in records if str(event.get("phase", "unknown")) == phase
                ).to_dict()
                for phase in phases
            },
            "by_iteration": {
                f"{phase}:{iteration}": self._totals(
                    event
                    for event in records
                    if str(event.get("phase", "unknown")) == phase
                    and event.get("iteration") == iteration
                ).to_dict()
                for phase, iteration in iterations
            },
        }

    def _totals(self, events: Iterable[dict[str, Any]]) -> _Totals:
        totals = _Totals()
        for event in events:
            totals.add(event, self._pricing)
        return totals
