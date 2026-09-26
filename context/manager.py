"""Context budget enforcement for the Executor's multi-round Agent Loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from context.compactor import (
    SUMMARY_MARKER,
    ContextCompactionError,
    ContextCompactor,
    ContextSummary,
)
from context.message_groups import MessageGroup, flatten_groups, group_messages
from context.token_estimator import TokenEstimate, TokenEstimator


ContextEventHandler = Callable[[dict[str, Any]], None]


class ContextHardLimitError(RuntimeError):
    """The prompt cannot be reduced below the configured hard input limit."""


@dataclass(frozen=True)
class ContextSnapshot:
    goal: str
    active_plan: str
    completed_work: list[str]
    unfinished_work: list[str]


@dataclass
class ContextSession:
    pinned_count: int
    summary: ContextSummary | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False


class ContextManager:
    def __init__(
        self,
        estimator: TokenEstimator,
        compactor: ContextCompactor,
        *,
        soft_limit: int,
        hard_limit: int,
        target_tokens: int,
        recent_groups: int = 2,
        failure_threshold: int = 2,
        summary_max_chars: int = 6000,
    ) -> None:
        if not 0 < target_tokens < soft_limit < hard_limit:
            raise ValueError("context limits must satisfy target < soft < hard")
        if recent_groups < 0:
            raise ValueError("recent_groups must be non-negative")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if summary_max_chars < 500:
            raise ValueError("summary_max_chars must be at least 500")
        self._estimator = estimator
        self._compactor = compactor
        self._soft_limit = soft_limit
        self._hard_limit = hard_limit
        self._target_tokens = target_tokens
        self._recent_groups = recent_groups
        self._failure_threshold = failure_threshold
        self._summary_max_chars = summary_max_chars

    def new_session(self, *, pinned_count: int) -> ContextSession:
        if pinned_count < 1:
            raise ValueError("at least the system prompt must be pinned")
        return ContextSession(pinned_count=pinned_count)

    def prepare(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session: ContextSession,
        snapshot: ContextSnapshot,
        *,
        on_context_event: ContextEventHandler | None = None,
        on_llm_event: ContextEventHandler | None = None,
        force_compaction: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a safe prompt before one main-model call."""
        if session.summary is not None:
            # Replanning can change the active contract between two calls. Keep
            # runtime-owned summary anchors current even when no new compaction runs.
            session.summary.goal = snapshot.goal
            session.summary.active_plan = snapshot.active_plan
            session.summary.completed_work = list(snapshot.completed_work)
            session.summary.unfinished_work = list(snapshot.unfinished_work)
        prefix, groups = self._split(messages, session)
        current = self._compose(prefix, groups, session.summary)
        initial = self._estimator.estimate(current, tools)
        self._emit(
            on_context_event,
            "context_estimated",
            tokens=initial.tokens,
            characters=initial.characters,
            method=initial.method,
            soft_limit=self._soft_limit,
            hard_limit=self._hard_limit,
            target_tokens=self._target_tokens,
        )
        if force_compaction:
            self._emit(
                on_context_event,
                "context_budget_forced",
                tokens=initial.tokens,
                reason="task_soft_token_budget",
            )
        if initial.tokens <= self._soft_limit and not force_compaction:
            self._emit_final(on_context_event, initial, action="none")
            return current

        if session.circuit_open:
            reduced, final = self._deterministic_trim(
                prefix, groups, session, snapshot, tools, on_context_event
            )
            self._enforce_hard_limit(final, on_context_event)
            self._emit_final(on_context_event, final, action="circuit_trim")
            return reduced

        compact_count = max(0, len(groups) - self._recent_groups)
        compactable = groups[:compact_count]
        recent = groups[compact_count:]
        if compactable:
            self._emit(
                on_context_event,
                "context_compaction_started",
                groups=len(compactable),
                recent_groups=len(recent),
                tokens_before=initial.tokens,
            )
            try:
                result = self._compactor.compact(
                    compactable,
                    goal=snapshot.goal,
                    active_plan=snapshot.active_plan,
                    completed_work=snapshot.completed_work,
                    unfinished_work=snapshot.unfinished_work,
                    existing_summary=session.summary,
                    on_llm_event=on_llm_event,
                    on_retry=lambda event: self._emit(
                        on_context_event, "context_compaction_retry", **event
                    ),
                )
            except ContextCompactionError as exc:
                session.consecutive_failures += 1
                self._emit(
                    on_context_event,
                    "context_compaction_failed",
                    attempts=exc.attempts,
                    omitted_groups=exc.omitted_groups,
                    consecutive_failures=session.consecutive_failures,
                    reason=str(exc),
                )
                if session.consecutive_failures >= self._failure_threshold:
                    session.circuit_open = True
                    self._emit(
                        on_context_event,
                        "context_circuit_opened",
                        consecutive_failures=session.consecutive_failures,
                    )
                if session.circuit_open or initial.tokens > self._hard_limit:
                    reduced, final = self._deterministic_trim(
                        prefix, groups, session, snapshot, tools, on_context_event
                    )
                    self._enforce_hard_limit(final, on_context_event)
                    self._emit_final(on_context_event, final, action="fallback_trim")
                    return reduced
                self._emit_final(on_context_event, initial, action="compaction_deferred")
                return current

            session.summary = result.summary
            session.consecutive_failures = 0
            compacted = self._compose(prefix, recent, session.summary)
            estimate = self._estimator.estimate(compacted, tools)
            self._emit(
                on_context_event,
                "context_compacted",
                attempts=result.attempts,
                omitted_groups=result.omitted_groups,
                groups_removed=len(compactable),
                tokens_before=initial.tokens,
                tokens_after=estimate.tokens,
            )
            if estimate.tokens > self._target_tokens:
                compacted, estimate = self._deterministic_trim(
                    prefix, recent, session, snapshot, tools, on_context_event
                )
            self._enforce_hard_limit(estimate, on_context_event)
            self._emit_final(on_context_event, estimate, action="compacted")
            return compacted

        # There is no old history that can be summarized without consuming the
        # configured recent-group reserve. Only an actual hard-limit risk permits
        # deterministic removal of those recent groups.
        if initial.tokens > self._hard_limit:
            reduced, final = self._deterministic_trim(
                prefix, groups, session, snapshot, tools, on_context_event
            )
            self._enforce_hard_limit(final, on_context_event)
            self._emit_final(on_context_event, final, action="emergency_trim")
            return reduced
        self._emit_final(on_context_event, initial, action="recent_groups_preserved")
        return current

    def observe_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response: Any,
        *,
        on_context_event: ContextEventHandler | None = None,
    ) -> None:
        usage = getattr(response, "usage", None)
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
        else:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
        if self._estimator.observe_actual(messages, tools, prompt_tokens):
            self._emit(
                on_context_event,
                "context_usage_calibrated",
                prompt_tokens=prompt_tokens,
            )

    def _deterministic_trim(
        self,
        prefix: list[dict[str, Any]],
        groups: list[MessageGroup],
        session: ContextSession,
        snapshot: ContextSnapshot,
        tools: list[dict[str, Any]],
        on_event: ContextEventHandler | None,
    ) -> tuple[list[dict[str, Any]], TokenEstimate]:
        kept = list(groups)
        removed: list[MessageGroup] = []
        summary = session.summary
        candidate = self._compose(prefix, kept, summary)
        estimate = self._estimator.estimate(candidate, tools)
        while estimate.tokens > self._target_tokens and kept:
            removed.append(kept.pop(0))
            summary = self._deterministic_summary(snapshot, session.summary, removed)
            candidate = self._compose(prefix, kept, summary)
            estimate = self._estimator.estimate(candidate, tools)
        if removed:
            session.summary = summary
        self._emit(
            on_event,
            "context_deterministic_trimmed",
            groups_removed=len(removed),
            groups_kept=len(kept),
            tokens_after=estimate.tokens,
            circuit_open=session.circuit_open,
        )
        return candidate, estimate

    def _deterministic_summary(
        self,
        snapshot: ContextSnapshot,
        existing: ContextSummary | None,
        removed: list[MessageGroup],
    ) -> ContextSummary:
        facts = list(existing.confirmed_facts if existing else [])
        errors = list(existing.important_errors if existing else [])
        for message in flatten_groups(removed):
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            bounded = content.strip()[:300]
            if "error" in bounded.lower() or "失败" in bounded:
                errors.append(bounded)
            else:
                facts.append(bounded)
        refs = list(existing.result_references if existing else [])
        refs.extend(self._compactor.extract_result_refs(removed))
        return ContextSummary(
            goal=snapshot.goal,
            active_plan=snapshot.active_plan,
            constraints=(
                list(existing.constraints if existing else [])
                + ["Older history was deterministically trimmed; use result_ref for details."]
            ),
            confirmed_facts=facts[-8:],
            result_references=list(dict.fromkeys(refs)),
            completed_work=list(snapshot.completed_work),
            unfinished_work=list(snapshot.unfinished_work),
            important_errors=errors[-6:],
        )

    def _split(
        self, messages: list[dict[str, Any]], session: ContextSession
    ) -> tuple[list[dict[str, Any]], list[MessageGroup]]:
        if len(messages) < session.pinned_count:
            raise ContextHardLimitError("context_missing_pinned_messages")
        prefix = [dict(message) for message in messages[: session.pinned_count]]
        history = [dict(message) for message in messages[session.pinned_count :]]
        history = [
            message
            for message in history
            if not (
                message.get("role") == "system"
                and isinstance(message.get("content"), str)
                and message["content"].startswith(SUMMARY_MARKER)
            )
        ]
        return prefix, group_messages(history)

    def _compose(
        self,
        prefix: list[dict[str, Any]],
        groups: list[MessageGroup],
        summary: ContextSummary | None,
    ) -> list[dict[str, Any]]:
        messages = [dict(message) for message in prefix]
        if summary is not None:
            messages.append(summary.to_message(self._summary_max_chars))
        messages.extend(flatten_groups(groups))
        return messages

    def _enforce_hard_limit(
        self, estimate: TokenEstimate, on_event: ContextEventHandler | None
    ) -> None:
        if estimate.tokens <= self._hard_limit:
            return
        self._emit(
            on_event,
            "context_hard_limit_exceeded",
            tokens=estimate.tokens,
            hard_limit=self._hard_limit,
        )
        raise ContextHardLimitError(
            f"context_hard_limit_exceeded: {estimate.tokens} > {self._hard_limit}"
        )

    def _emit_final(
        self,
        on_event: ContextEventHandler | None,
        estimate: TokenEstimate,
        *,
        action: str,
    ) -> None:
        self._emit(
            on_event,
            "context_final_size",
            tokens=estimate.tokens,
            characters=estimate.characters,
            method=estimate.method,
            action=action,
        )

    @staticmethod
    def _emit(
        handler: ContextEventHandler | None, kind: str, **fields: Any
    ) -> None:
        if handler is None:
            return
        handler(
            {
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                **fields,
            }
        )
