"""Cumulative Agent Loop token budgets built on persisted usage snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from task.task_manager import TaskManager

from .meter import UsageMeter


class TokenBudgetExceededError(RuntimeError):
    """Raised once cumulative budgeted model usage reaches the hard limit."""


@dataclass(frozen=True)
class TokenBudgetPolicy:
    soft_tokens: int
    hard_tokens: int

    def __post_init__(self) -> None:
        if not 0 < self.soft_tokens < self.hard_tokens:
            raise ValueError("task token budget must satisfy 0 < soft < hard")


class BudgetController:
    """Convert cumulative usage into convergence and termination decisions."""

    def __init__(
        self,
        task_manager: TaskManager,
        meter: UsageMeter,
        policy: TokenBudgetPolicy,
    ) -> None:
        self._task_manager = task_manager
        self._meter = meter
        self._policy = policy

    def before_call(
        self, task_id: str, *, phase: str, iteration: int | None = None
    ) -> bool:
        """Fail before more spend, or request context convergence after soft limit."""
        return self._evaluate(task_id, phase=phase, iteration=iteration)

    def after_call(
        self, task_id: str, *, phase: str, iteration: int | None = None
    ) -> bool:
        """Reject the response that first reaches the hard cumulative limit."""
        return self._evaluate(task_id, phase=phase, iteration=iteration)

    def _evaluate(self, task_id: str, *, phase: str, iteration: int | None) -> bool:
        total = int(self._meter.snapshot(task_id).get("budgeted_total_tokens", 0))
        if total >= self._policy.hard_tokens:
            self._record_once(
                task_id,
                "token_hard_budget_exceeded",
                phase=phase,
                iteration=iteration,
                total_tokens=total,
                soft_tokens=self._policy.soft_tokens,
                hard_tokens=self._policy.hard_tokens,
            )
            raise TokenBudgetExceededError(
                f"task_token_hard_budget_exceeded: {total} >= {self._policy.hard_tokens}"
            )
        if total >= self._policy.soft_tokens:
            self._record_once(
                task_id,
                "token_soft_budget_reached",
                phase=phase,
                iteration=iteration,
                total_tokens=total,
                soft_tokens=self._policy.soft_tokens,
                hard_tokens=self._policy.hard_tokens,
            )
            return True
        return False

    def _record_once(self, task_id: str, kind: str, **fields: Any) -> None:
        task = self._task_manager.get_task(task_id)
        if any(
            isinstance(event, dict) and event.get("kind") == kind
            for event in task.metrics.get("budget_events", [])
        ):
            return
        self._task_manager.add_metric_events(task_id, "budget_events", [{
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **fields,
        }])
