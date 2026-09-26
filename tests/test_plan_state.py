"""P0-2: the runtime view of a plan, derived from the append-only plan events."""

import pytest

from task.plan_model import Plan, PlanValidationError
from task.plan_state import PlanRunState, PlanStepStatus


def step(step_id, depends_on=(), tool=None):
    return {
        "step_id": step_id,
        "name": step_id,
        "description": f"do {step_id}",
        "tool": tool,
        "depends_on": list(depends_on),
        "completion_criteria": [f"{step_id} evidence"],
    }


def make_plan(steps, version=1, user_input="summarize sales"):
    return Plan.from_dict({"user_input": user_input, "version": version, "steps": steps})


def bound(step_id, version=1, implicit=False):
    return {
        "kind": "plan_step_bound",
        "step_id": step_id,
        "version": version,
        "implicit": implicit,
    }


def completed(step_id, version=1, evidence="evidence"):
    return {
        "kind": "plan_step_completed",
        "step_id": step_id,
        "version": version,
        "evidence": evidence,
    }


def failed_event(step_id, version=1, error="boom"):
    return {"kind": "plan_step_failed", "step_id": step_id, "version": version, "error": error}


def two_steps():
    return make_plan([step("read"), step("report", ["read"])])


def test_status_is_derived_from_binding_and_completion_events():
    plan = two_steps()
    state = PlanRunState(plan, [bound("read"), completed("read")])

    assert state.status("read") is PlanStepStatus.SUCCESS
    assert state.status("report") is PlanStepStatus.PENDING
    assert state.completed_ids() == ["read"]
    assert state.unfinished_ids() == ["report"]
    assert state.all_complete() is False


def test_binding_moves_a_step_to_running_without_completing_it():
    state = PlanRunState(two_steps(), [bound("read")])

    assert state.status("read") is PlanStepStatus.RUNNING
    assert state.running_ids() == ["read"]
    assert state.ready_ids() == []
    assert state.all_complete() is False


def test_dependent_step_is_not_ready_until_its_dependency_is_complete():
    plan = two_steps()

    blocked = PlanRunState(plan, [])
    assert blocked.ready_ids() == ["read"]
    assert blocked.unsatisfied_dependencies("report") == ["read"]

    released = PlanRunState(plan, [completed("read")])
    assert released.ready_ids() == ["report"]
    assert released.unsatisfied_dependencies("report") == []


def test_completed_step_is_never_ready_again():
    state = PlanRunState(two_steps(), [completed("read")])

    assert "read" not in state.ready_ids()
    assert "read" not in state.running_ids()
    assert state.is_complete("read") is True


def test_failed_step_stays_schedulable_so_the_model_can_change_route():
    state = PlanRunState(two_steps(), [bound("read"), failed_event("read", error="no such file")])

    assert state.status("read") is PlanStepStatus.FAILED
    assert state.ready_ids() == ["read"]
    assert state.is_complete("read") is False


def test_events_of_steps_that_left_the_plan_are_ignored():
    new_plan = make_plan([step("analyze")], version=2)
    state = PlanRunState(new_plan, [completed("read", version=1), completed("analyze", version=2)])

    assert state.step_ids() == ["analyze"]
    assert state.status("analyze") is PlanStepStatus.SUCCESS
    assert state.all_complete() is True


def test_replan_keeps_completed_steps_complete_in_the_new_version():
    old_plan = two_steps()
    events = [bound("read", version=1), completed("read", version=1)]
    new_plan = make_plan([step("read"), step("report_v2", ["read"])], version=2)

    state = PlanRunState(new_plan, events)

    assert state.version == 2
    assert state.status("read") is PlanStepStatus.SUCCESS
    assert state.ready_ids() == ["report_v2"]


def test_failure_observations_are_reported_for_the_planner():
    state = PlanRunState(
        two_steps(),
        [bound("read"), failed_event("read", error="workbook missing")],
    )

    assert state.failure_observations() == [
        {"step_id": "read", "error": "workbook missing", "error_type": None}
    ]
    assert state.failure("read")["error"] == "workbook missing"


def test_evidence_is_kept_for_each_completed_step():
    state = PlanRunState(two_steps(), [completed("read", evidence="A and B columns found")])

    assert state.evidence("read") == ["A and B columns found"]
    assert state.evidence("report") == []


def test_an_invalid_plan_never_becomes_a_runtime_state():
    with pytest.raises(PlanValidationError):
        PlanRunState(Plan(user_input="x"), [])


def test_unknown_step_never_raises_but_is_reported_as_pending():
    state = PlanRunState(two_steps(), [])

    assert state.status("missing") is PlanStepStatus.PENDING
    assert state.is_complete("missing") is False
