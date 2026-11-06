import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from generaldataagent.contracts.rubric import (
    RUBRIC_VERIFIER_IDS,
    RubricCheckResult,
    RubricDeliveryRequirement,
    RubricEvaluation,
    RubricMethodRequirement,
    RubricSourceRequirement,
    TaskRubric,
    evaluation_payload,
    rubric_payload,
)
from generaldataagent.contracts.rubric_display import RubricDisplay, RubricProvenance
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.review.rubric_display import (
    RubricDisplayRefused,
    render_rubric_display,
)

START = "2026-11-01"
END = "2026-11-02"
METHOD_ID = "metric.certified"
METHOD_VERSION = "v1"
VERIFIERS = tuple(sorted(RUBRIC_VERIFIER_IDS))
CHECK_IDS = {
    "rubric.required_checks_present": "a-required",
    "rubric.time_range_bound": "b-time",
    "rubric.metric_version_bound": "c-metric",
    "rubric.sources_single_snapshot": "d-sources",
    "rubric.claims_grounded": "e-claims",
    "rubric.table_and_chart_evidence": "f-evidence",
    "rubric.human_items_acknowledged": "g-human",
}


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def rubric_for(*, task_id=None, metric_version="v1", human_items=()) -> TaskRubric:
    task = task_id or uuid4()
    sources = (
        RubricSourceRequirement(source_id="orders", content_hash=digest("orders-v1"), version="v1"),
    )
    method = RubricMethodRequirement(method_id=METHOD_ID, method_version=METHOD_VERSION)
    delivery = RubricDeliveryRequirement(
        required_claim_types=("fact", "limitation"),
        human_confirmation_items=human_items,
        verifier_ids=VERIFIERS,
    )
    payload = rubric_payload(
        rubric_id="weekly-report",
        task_id=task,
        metric_name="weekly_sales",
        metric_version=metric_version,
        start_time=START,
        end_time=END,
        sources=sources,
        method=method,
        delivery=delivery,
    )
    return TaskRubric(
        rubric_id="weekly-report",
        task_id=task,
        metric_name="weekly_sales",
        metric_version=metric_version,
        start_time=START,
        end_time=END,
        sources=sources,
        method=method,
        delivery=delivery,
        rubric_digest=canonical_hash(payload),
    )


def check(verifier_id: str, outcome="satisfied", *, code=None) -> RubricCheckResult:
    return RubricCheckResult(
        check_id=CHECK_IDS[verifier_id],
        verifier_id=verifier_id,
        outcome=outcome,
        code=code or ("satisfied" if outcome == "satisfied" else outcome),
        message=f"deterministic result for {verifier_id}",
        evidence_digest=digest(verifier_id) if outcome == "satisfied" else None,
    )


def evaluation_for(rubric: TaskRubric, outcomes=None) -> RubricEvaluation:
    selected = dict(outcomes or {})
    checks = tuple(
        check(verifier_id, selected.get(verifier_id, "satisfied")) for verifier_id in VERIFIERS
    )
    return _evaluation_with(rubric, checks)


def _evaluation_with(rubric: TaskRubric, checks: tuple[RubricCheckResult, ...]) -> RubricEvaluation:
    ordered = tuple(sorted(checks, key=lambda item: item.check_id))
    gaps = any(item.outcome == "gap" for item in ordered)
    humans = any(item.outcome == "needs_human" for item in ordered)
    payload = evaluation_payload(
        task_id=rubric.task_id,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.rubric_digest,
        checks=ordered,
    )
    return RubricEvaluation(
        task_id=rubric.task_id,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.rubric_digest,
        verdict="fail" if gaps else "needs_human" if humans else "pass",
        checks=ordered,
        evaluation_digest=canonical_hash(payload),
    )


def test_all_satisfied_is_pass_with_producer_self_check_only():
    rubric = rubric_for()
    display = render_rubric_display(rubric, evaluation_for(rubric))

    assert display.verdict == "pass"
    assert display.check_count == 7
    assert len(display.satisfied) == 7
    assert display.gaps == ()
    assert display.needs_human == ()
    assert display.provenance.producer_self_check is True
    assert display.provenance.independent_recheck == "not_run"
    assert display.provenance.human_signoff == "not_required"
    assert display.provenance.human_items == ()
    assert display.provenance.model_commentary_authoritative is False
    assert display.display_digest.startswith("sha256:")


def test_gap_and_human_are_reported_in_separate_buckets():
    rubric = rubric_for(human_items=("business_owner_signoff",))
    evaluation = evaluation_for(
        rubric,
        {
            "rubric.time_range_bound": "gap",
            "rubric.human_items_acknowledged": "needs_human",
        },
    )

    display = render_rubric_display(rubric, evaluation)

    assert display.verdict == "fail"
    assert [item.verifier_id for item in display.gaps] == ["rubric.time_range_bound"]
    assert [item.verifier_id for item in display.needs_human] == ["rubric.human_items_acknowledged"]
    assert len(display.satisfied) == 5
    assert display.provenance.human_signoff == "required"
    assert display.provenance.human_items == ("business_owner_signoff",)


def test_needs_human_only_is_not_a_pass_or_a_gap():
    rubric = rubric_for(human_items=("operations_owner_signoff",))
    display = render_rubric_display(
        rubric,
        evaluation_for(rubric, {"rubric.human_items_acknowledged": "needs_human"}),
    )

    assert display.verdict == "needs_human"
    assert display.gaps == ()
    assert len(display.needs_human) == 1
    assert display.provenance.human_signoff == "required"


def test_satisfied_attestation_is_labelled_as_human_signoff():
    rubric = rubric_for(human_items=("channel_owner_signoff",))
    display = render_rubric_display(rubric, evaluation_for(rubric))

    assert display.verdict == "pass"
    assert display.provenance.human_signoff == "satisfied"
    assert display.provenance.human_items == ("channel_owner_signoff",)


def test_absent_human_items_are_not_required():
    rubric = rubric_for()
    display = render_rubric_display(
        rubric,
        evaluation_for(
            rubric,
            {"rubric.human_items_acknowledged": "satisfied"},
        ),
    )

    assert display.provenance.human_signoff == "not_required"
    assert display.provenance.human_items == ()


def test_other_task_or_rubric_is_refused():
    rubric = rubric_for()
    other_task = rubric_for()
    with pytest.raises(RubricDisplayRefused, match="rubric_display_task_mismatch"):
        render_rubric_display(rubric, evaluation_for(other_task))

    changed = rubric_for(task_id=rubric.task_id, metric_version="v2")
    with pytest.raises(RubricDisplayRefused, match="rubric_display_rubric_changed"):
        render_rubric_display(rubric, evaluation_for(changed))


def test_incomplete_duplicate_or_missing_human_checks_are_refused():
    rubric = rubric_for()
    six = tuple(item for item in evaluation_for(rubric).checks if item.verifier_id != VERIFIERS[0])
    incomplete = _evaluation_with(rubric, six)
    with pytest.raises(RubricDisplayRefused, match="rubric_display_verifier_set_mismatch"):
        render_rubric_display(rubric, incomplete)

    duplicated = tuple(
        item for item in evaluation_for(rubric).checks if item.verifier_id != VERIFIERS[2]
    ) + (
        RubricCheckResult(
            check_id="h-required",
            verifier_id=VERIFIERS[0],
            outcome="satisfied",
            code="satisfied",
            message="duplicate verifier declaration",
        ),
    )
    with pytest.raises(RubricDisplayRefused, match="rubric_display_verifier_set_mismatch"):
        render_rubric_display(rubric, _evaluation_with(rubric, duplicated))

    no_human = tuple(
        item
        for item in evaluation_for(rubric).checks
        if item.verifier_id != "rubric.human_items_acknowledged"
    )
    with pytest.raises(RubricDisplayRefused, match="rubric_display_human_check_missing"):
        render_rubric_display(rubric, _evaluation_with(rubric, no_human))


def test_human_check_cannot_be_a_gap():
    rubric = rubric_for(human_items=("business_owner_signoff",))
    evaluation = evaluation_for(rubric, {"rubric.human_items_acknowledged": "gap"})

    with pytest.raises(RubricDisplayRefused, match="rubric_display_human_check_invalid"):
        render_rubric_display(rubric, evaluation)


def test_display_digest_tamper_is_refused():
    rubric = rubric_for()
    display = render_rubric_display(rubric, evaluation_for(rubric))
    data = display.model_dump()
    data["display_digest"] = digest("tampered")

    with pytest.raises(ValidationError, match="rubric_display_digest_mismatch"):
        RubricDisplay(**data)


def test_group_status_and_count_tamper_are_refused():
    rubric = rubric_for(human_items=("business_owner_signoff",))
    display = render_rubric_display(
        rubric,
        evaluation_for(rubric, {"rubric.time_range_bound": "gap"}),
    )

    mismatched = display.model_dump()
    mismatched["gaps"][0]["status"] = "satisfied"
    with pytest.raises(ValidationError, match="rubric_display_status_mismatch"):
        RubricDisplay(**mismatched)

    miscounted = display.model_dump()
    miscounted["check_count"] = 6
    with pytest.raises(ValidationError, match="rubric_display_check_count_mismatch"):
        RubricDisplay(**miscounted)


def test_unsorted_or_duplicated_items_are_refused():
    rubric = rubric_for()
    display = render_rubric_display(rubric, evaluation_for(rubric))
    data = display.model_dump()
    data["satisfied"] = list(data["satisfied"])
    data["satisfied"][0], data["satisfied"][1] = data["satisfied"][1], data["satisfied"][0]
    with pytest.raises(ValidationError, match="rubric_display_checks_unordered"):
        RubricDisplay(**data)

    duplicate = display.model_dump()
    duplicate["gaps"] = [dict(duplicate["satisfied"][0], status="gap")]
    duplicate["check_count"] = 8
    with pytest.raises(ValidationError, match="rubric_display_duplicate_check"):
        RubricDisplay(**duplicate)


def test_provenance_refuses_inconsistent_signoff():
    with pytest.raises(ValidationError, match="rubric_display_human_signoff_invalid"):
        RubricProvenance(human_signoff="required", human_items=())
    with pytest.raises(ValidationError, match="rubric_display_human_signoff_invalid"):
        RubricProvenance(human_signoff="not_required", human_items=("owner",))
    with pytest.raises(ValidationError, match="rubric_display_duplicate_human_item"):
        RubricProvenance(human_signoff="required", human_items=("owner", "owner"))


def test_model_commentary_cannot_enter_or_claim_independence():
    rubric = rubric_for()
    display = render_rubric_display(rubric, evaluation_for(rubric))

    authoritative = display.model_dump()
    authoritative["provenance"]["model_commentary_authoritative"] = True
    with pytest.raises(ValidationError):
        RubricDisplay(**authoritative)

    claimed = display.model_dump()
    claimed["provenance"]["independent_recheck"] = "passed"
    with pytest.raises(ValidationError):
        RubricDisplay(**claimed)

    extra = display.model_dump()
    extra["model_commentary"] = "Everything is perfect."
    with pytest.raises(ValidationError):
        RubricDisplay(**extra)


def test_rerender_is_deterministic():
    rubric = rubric_for(human_items=("business_owner_signoff",))
    evaluation = evaluation_for(rubric, {"rubric.time_range_bound": "gap"})

    first = render_rubric_display(rubric, evaluation)
    second = render_rubric_display(rubric, evaluation)

    assert first == second
