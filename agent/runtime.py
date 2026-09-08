"""Per-task context. ContextVar keeps concurrent worker threads independent."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class RunContext:
    task_id: str
    manager: Any
    deadline: float
    max_tool_calls: int
    workspace: str = ''
    report_prefix: str = ''
    requirements: dict = field(default_factory=dict)
    tool_calls: int = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded('task_deadline_exceeded')

    def consume_tool(self):
        self.check()
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded('tool_budget_exceeded')
        self.tool_calls += 1


current_run: ContextVar[RunContext | None] = ContextVar('current_run', default=None)


@contextmanager
def run_scope(context):
    token = current_run.set(context)
    try:
        yield context
    finally:
        current_run.reset(token)


def check_budget():
    context = current_run.get()
    if context:
        context.check()

