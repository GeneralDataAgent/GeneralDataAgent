"""Build the deterministic satisfied/gap/needs_human view of a rubric result.

This module only regroups a validated ``RubricEvaluation``. It refuses an
evaluation for another task, rubric or rubric version, an incomplete or
duplicated verifier set, a human-attestation check that is not one of its two
allowed outcomes, and any attempt to supply model text as authority. It has no
model call, write, repair, revision or publication path.
"""

from __future__ import annotations

from generaldataagent.contracts.rubric import (
    RubricCheckResult,
    RubricEvaluation,
    TaskRubric,
)
from generaldataagent.contracts.rubric_display import (
    RubricDisplay,
    RubricDisplayItem,
    RubricProvenance,
    display_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash

__all__ = ["RubricDisplayRefused", "render_rubric_display"]

_HUMAN_VERIFIER = "rubric.human_items_acknowledged"


class RubricDisplayRefused(ValueError):
    """The evaluation cannot be displayed without hiding or inventing a state."""


def _item(check: RubricCheckResult) -> RubricDisplayItem:
    return RubricDisplayItem(
        status=check.outcome,
        check_id=check.check_id,
        verifier_id=check.verifier_id,
        code=check.code,
        message=check.message,
        evidence_digest=check.evidence_digest,
    )


def render_rubric_display(rubric: TaskRubric, evaluation: RubricEvaluation) -> RubricDisplay:
    """Group every check exactly once and state only the provenance that exists."""
    if not isinstance(rubric, TaskRubric) or not isinstance(evaluation, RubricEvaluation):
        raise RubricDisplayRefused("rubric_display_input_invalid")
    if evaluation.task_id != rubric.task_id:
        raise RubricDisplayRefused("rubric_display_task_mismatch")
    if evaluation.rubric_id != rubric.rubric_id:
        raise RubricDisplayRefused("rubric_display_rubric_mismatch")
    if evaluation.rubric_digest != rubric.rubric_digest:
        raise RubricDisplayRefused("rubric_display_rubric_changed")

    human = next(
        (check for check in evaluation.checks if check.verifier_id == _HUMAN_VERIFIER), None
    )
    if human is None:
        raise RubricDisplayRefused("rubric_display_human_check_missing")
    declared = tuple(rubric.delivery.verifier_ids)
    observed = tuple(check.verifier_id for check in evaluation.checks)
    if len(observed) != len(declared) or set(observed) != set(declared):
        raise RubricDisplayRefused("rubric_display_verifier_set_mismatch")
    if human.outcome == "gap":
        raise RubricDisplayRefused("rubric_display_human_check_invalid")

    human_items = rubric.delivery.human_confirmation_items
    if not human_items:
        signoff = "not_required"
    elif human.outcome == "satisfied":
        signoff = "satisfied"
    else:
        signoff = "required"
    provenance = RubricProvenance(human_signoff=signoff, human_items=human_items)

    items = tuple(_item(check) for check in evaluation.checks)
    satisfied = tuple(item for item in items if item.status == "satisfied")
    gaps = tuple(item for item in items if item.status == "gap")
    needs_human = tuple(item for item in items if item.status == "needs_human")
    verdict = "fail" if gaps else "needs_human" if needs_human else "pass"
    check_count = len(items)
    payload = display_payload(
        task_id=evaluation.task_id,
        rubric_id=evaluation.rubric_id,
        rubric_digest=evaluation.rubric_digest,
        evaluation_digest=evaluation.evaluation_digest,
        verdict=verdict,
        satisfied=satisfied,
        gaps=gaps,
        needs_human=needs_human,
        check_count=check_count,
        provenance=provenance,
    )
    return RubricDisplay(
        task_id=evaluation.task_id,
        rubric_id=evaluation.rubric_id,
        rubric_digest=evaluation.rubric_digest,
        evaluation_digest=evaluation.evaluation_digest,
        verdict=verdict,
        check_count=check_count,
        satisfied=satisfied,
        gaps=gaps,
        needs_human=needs_human,
        provenance=provenance,
        display_digest=canonical_hash(payload),
    )
