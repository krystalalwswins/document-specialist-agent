"""P1-1: deterministic policy keeps guesses and secrets out of memory."""

from memory.model import MemoryCandidate, MemorySourceKind, MemoryType
from memory.policy import MemoryPolicy


def _candidate(
    content="Prefer concise reports",
    *,
    memory_type=MemoryType.PREFERENCE,
    confidence=0.9,
    source_kind=MemorySourceKind.USER_EXPLICIT,
    evidence="I prefer concise reports",
):
    return MemoryCandidate(memory_type, content, confidence, source_kind, evidence)


def _evaluate(candidate, *, user_input="I prefer concise reports", verified=()):
    return MemoryPolicy(min_confidence=0.8).evaluate(
        candidate,
        user_id="alice",
        project_id="sales",
        source_task_id="task-1",
        user_input=user_input,
        verified_evidence=list(verified),
    )


def test_explicit_supported_preference_is_accepted_with_provenance():
    decision = _evaluate(_candidate())

    assert decision.accepted is True
    assert decision.record.source_task_id == "task-1"
    assert decision.record.source_excerpt == "I prefer concise reports"


def test_model_guess_without_source_excerpt_is_rejected():
    decision = _evaluate(_candidate(evidence="The user probably likes short reports"))

    assert decision.accepted is False
    assert decision.reason == "evidence_not_found_in_declared_source"


def test_low_confidence_and_sensitive_candidates_are_rejected():
    low = _evaluate(_candidate(confidence=0.4))
    secret = _evaluate(
        _candidate(
            content="api_key=sk_abcdefghijklmnop",
            evidence="api_key=sk_abcdefghijklmnop",
        ),
        user_input="api_key=sk_abcdefghijklmnop",
    )

    assert low.reason == "confidence_below_threshold"
    assert secret.reason == "sensitive_content"


def test_preference_requires_user_source_and_procedure_requires_verified_source():
    preference = _evaluate(
        _candidate(source_kind=MemorySourceKind.TASK_VERIFIED),
        verified=["I prefer concise reports"],
    )
    procedure = _evaluate(
        _candidate(
            content="Validate the workbook before summarizing",
            memory_type=MemoryType.PROCEDURE,
            source_kind=MemorySourceKind.TASK_VERIFIED,
            evidence="Workbook validation passed",
        ),
        verified=["Workbook validation passed"],
    )

    assert preference.reason == "preference_or_constraint_requires_user_explicit_source"
    assert procedure.accepted is True
