"""Deterministic control of at most two rubric-bound revisions.

The controller does not revise anything. It answers one question outside the
model loop: may the caller spend one more revision on this exact delivery,
under this exact rubric and this remaining budget? The answer is derived from a
validated ``RubricEvaluation``, never from model text.

Rules fixed by this slice:

* the rubric digest is pinned at construction; an evaluation for another task,
  rubric or rubric version is refused rather than treated as a fresh start;
* only a ``gap`` check may trigger a revision. ``pass`` stops as satisfied and
  ``needs_human`` stops for human review; neither consumes a revision;
* no-source, no-capability and authorization-denied causes stop immediately and
  do not consume a revision;
* a revision needs positive remaining time, token and tool-call budget;
* the input digest and evidence set must match the current retained version, and
  every produced output must be recorded before another revision is requested.
  The resulting ``RevisionLedger`` retains every input, output and evidence set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from generaldataagent.contracts.revision import (
    MAX_REVISIONS,
    RevisionBudget,
    RevisionLedger,
    RevisionVersion,
    ledger_payload,
    ordered_evidence,
)
from generaldataagent.contracts.rubric import RubricEvaluation, TaskRubric
from generaldataagent.memory.display_preferences import canonical_hash

__all__ = ["BoundedRevisionController", "RevisionDecision", "RevisionRefused"]

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPLICIT_CAUSE = {
    "no_source": "source",
    "no_capability": "capability",
    "authorization_denied": "authorization",
}
# Fixed order keeps the stop reason deterministic when several budgets are spent.
_BUDGETS = (
    ("time", "remaining_seconds"),
    ("token", "remaining_tokens"),
    ("tool", "remaining_tool_calls"),
)


class RevisionRefused(ValueError):
    """The request is inconsistent with the pinned rubric or retained versions."""


@dataclass(frozen=True, slots=True)
class RevisionDecision:
    """One answer: either authorization for ``attempt``, or a terminal stop.

    ``attempt`` is the revision being authorized when ``allowed`` is true, and
    the number of revisions already consumed when it is false.
    """

    allowed: bool
    reason: str
    attempt: int
    failure_code: str
    cause: str
    rubric_digest: str


def _require_digest(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.match(value):
        raise RevisionRefused(f"{name}_invalid")
    return value


def _require_evidence(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise RevisionRefused("revision_evidence_invalid")
    if values != ordered_evidence(values):
        raise RevisionRefused("revision_evidence_unordered")
    for value in values:
        _require_digest(value, name="revision_evidence_digest")
    return values


class BoundedRevisionController:
    """Stateful, fail-closed revision gate for one task and one rubric version."""

    def __init__(
        self,
        rubric: TaskRubric,
        *,
        initial_delivery_digest: str,
        initial_evidence_digests: tuple[str, ...] = (),
        max_revisions: int = MAX_REVISIONS,
    ) -> None:
        if not isinstance(rubric, TaskRubric):
            raise RevisionRefused("revision_rubric_invalid")
        if type(max_revisions) is not int or not 1 <= max_revisions <= MAX_REVISIONS:
            raise RevisionRefused("revision_limit_invalid")
        self._rubric_id = rubric.rubric_id
        self._task_id = rubric.task_id
        self._rubric_digest = rubric.rubric_digest
        self._max_revisions = max_revisions
        self._delivery_digest = _require_digest(
            initial_delivery_digest, name="revision_initial_digest"
        )
        self._evidence_digests = _require_evidence(initial_evidence_digests)
        self._versions: list[RevisionVersion] = []
        self._pending: RevisionDecision | None = None
        self._stop_reason: str | None = None

    @property
    def rubric_digest(self) -> str:
        return self._rubric_digest

    @property
    def current_digest(self) -> str:
        return self._delivery_digest

    @property
    def current_evidence_digests(self) -> tuple[str, ...]:
        return self._evidence_digests

    def request(
        self,
        evaluation: RubricEvaluation,
        *,
        check_id: str,
        budget: RevisionBudget,
        input_digest: str,
        input_evidence_digests: tuple[str, ...],
    ) -> RevisionDecision:
        """Ask whether the selected gap check may consume one revision."""
        if not isinstance(evaluation, RubricEvaluation):
            raise RevisionRefused("revision_evaluation_invalid")
        if not isinstance(budget, RevisionBudget):
            raise RevisionRefused("revision_budget_invalid")
        if not isinstance(check_id, str) or not check_id.strip():
            raise RevisionRefused("revision_check_required")
        if evaluation.task_id != self._task_id:
            raise RevisionRefused("revision_task_mismatch")
        if evaluation.rubric_id != self._rubric_id:
            raise RevisionRefused("revision_rubric_mismatch")
        if evaluation.rubric_digest != self._rubric_digest:
            raise RevisionRefused("revision_rubric_changed")
        if self._pending is not None:
            raise RevisionRefused("revision_output_not_recorded")
        if _require_digest(input_digest, name="revision_input_digest") != self._delivery_digest:
            raise RevisionRefused("revision_input_not_current")
        if _require_evidence(input_evidence_digests) != self._evidence_digests:
            raise RevisionRefused("revision_evidence_not_current")

        if evaluation.verdict == "pass":
            return self._stop("revision_rubric_satisfied")
        if evaluation.verdict == "needs_human":
            return self._stop("revision_requires_human")

        check = next((item for item in evaluation.checks if item.check_id == check_id), None)
        if check is None:
            raise RevisionRefused("revision_check_unknown")
        if check.outcome != "gap":
            raise RevisionRefused("revision_check_not_gap")

        code = check.code
        # Function-local: the runtime review package must not import the offline
        # evaluation package at module level (see test_evaluation_import_boundary).
        from generaldataagent.evaluation.routing import failure_cause, should_retry

        cause = _EXPLICIT_CAUSE.get(code, failure_cause(code))
        if code in _EXPLICIT_CAUSE or not should_retry(code):
            return self._stop(f"revision_not_applicable_{cause}", code=code, cause=cause)
        if len(self._versions) >= self._max_revisions:
            return self._stop("revision_limit_reached", code=code, cause=cause)
        for name, attribute in _BUDGETS:
            if getattr(budget, attribute) <= 0:
                return self._stop(f"revision_{name}_budget_exhausted", code=code, cause=cause)

        decision = RevisionDecision(
            allowed=True,
            reason="revision_allowed",
            attempt=len(self._versions) + 1,
            failure_code=code,
            cause=cause,
            rubric_digest=self._rubric_digest,
        )
        self._pending = decision
        return decision

    def record(
        self,
        *,
        output_digest: str,
        output_evidence_digests: tuple[str, ...] = (),
    ) -> RevisionVersion:
        """Retain the produced version; required before another request."""
        decision = self._pending
        if decision is None:
            raise RevisionRefused("revision_not_authorized")
        output_digest = _require_digest(output_digest, name="revision_output_digest")
        if output_digest == self._delivery_digest:
            raise RevisionRefused("revision_output_unchanged")
        evidence = _require_evidence(output_evidence_digests)
        version = RevisionVersion(
            attempt=decision.attempt,
            failure_code=decision.failure_code,
            cause=decision.cause,
            input_digest=self._delivery_digest,
            output_digest=output_digest,
            input_evidence_digests=self._evidence_digests,
            output_evidence_digests=evidence,
        )
        self._versions.append(version)
        self._delivery_digest = output_digest
        self._evidence_digests = evidence
        self._pending = None
        return version

    def ledger(self) -> RevisionLedger:
        """The immutable record of all retained versions and the final stop."""
        if self._pending is not None:
            raise RevisionRefused("revision_output_not_recorded")
        stopped_reason = self._stop_reason or "revision_open"
        versions = tuple(self._versions)
        payload = ledger_payload(
            rubric_id=self._rubric_id,
            task_id=self._task_id,
            rubric_digest=self._rubric_digest,
            initial_delivery_digest=(
                versions[0].input_digest if versions else self._delivery_digest
            ),
            initial_evidence_digests=(
                versions[0].input_evidence_digests if versions else self._evidence_digests
            ),
            versions=versions,
            stopped_reason=stopped_reason,
        )
        return RevisionLedger(
            rubric_id=self._rubric_id,
            task_id=self._task_id,
            rubric_digest=self._rubric_digest,
            initial_delivery_digest=payload["initial_delivery_digest"],
            initial_evidence_digests=tuple(payload["initial_evidence_digests"]),
            versions=versions,
            revision_count=len(versions),
            stopped_reason=stopped_reason,
            ledger_digest=canonical_hash(payload),
        )

    def report(self) -> dict:
        return {
            "revisions": len(self._versions),
            "max_revisions": self._max_revisions,
            "stopped_reason": self._stop_reason,
            "pending_record": self._pending is not None,
        }

    def _stop(
        self,
        reason: str,
        *,
        code: str = "",
        cause: str = "",
    ) -> RevisionDecision:
        self._stop_reason = reason
        return RevisionDecision(
            allowed=False,
            reason=reason,
            attempt=len(self._versions),
            failure_code=code,
            cause=cause,
            rubric_digest=self._rubric_digest,
        )
