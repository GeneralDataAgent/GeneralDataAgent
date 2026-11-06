import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from generaldataagent.contracts.revision import (
    MAX_REVISIONS,
    RevisionBudget,
    RevisionLedger,
    RevisionVersion,
    ledger_payload,
)
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
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.review.bounded_revision import (
    BoundedRevisionController,
    RevisionRefused,
)

START = "2026-11-01"
END = "2026-11-02"
METHOD_ID = "metric.certified"
METHOD_VERSION = "v1"
VERIFIERS = tuple(sorted(RUBRIC_VERIFIER_IDS))


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


def evaluation_for(rubric: TaskRubric, *checks: RubricCheckResult) -> RubricEvaluation:
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


def check(
    *,
    check_id="gap.source",
    outcome="gap",
    code="missing_evidence",
    verifier_id="rubric.sources_single_snapshot",
) -> RubricCheckResult:
    return RubricCheckResult(
        check_id=check_id,
        verifier_id=verifier_id,
        outcome=outcome,
        code=code,
        message="deterministic rubric check",
    )


def budget_ok() -> RevisionBudget:
    return RevisionBudget(remaining_seconds=60, remaining_tokens=1_000, remaining_tool_calls=3)


def controller_for(rubric: TaskRubric, *, evidence=None) -> BoundedRevisionController:
    initial = (digest("initial"),) if evidence is None else evidence
    return BoundedRevisionController(
        rubric,
        initial_delivery_digest=digest("delivery-0"),
        initial_evidence_digests=tuple(sorted(initial)),
    )


def ask(
    controller: BoundedRevisionController,
    evaluation: RubricEvaluation,
    *,
    check_id="gap.source",
    budget=None,
    input_digest=None,
    evidence=None,
):
    return controller.request(
        evaluation,
        check_id=check_id,
        budget=budget or budget_ok(),
        input_digest=input_digest or controller.current_digest,
        input_evidence_digests=(
            controller.current_evidence_digests if evidence is None else evidence
        ),
    )


def test_allowed_revision_retains_input_output_and_evidence():
    rubric = rubric_for()
    controller = controller_for(rubric)
    evaluation = evaluation_for(rubric, check())

    decision = ask(controller, evaluation)

    assert decision.allowed
    assert decision.attempt == 1
    assert decision.reason == "revision_allowed"
    version = controller.record(
        output_digest=digest("delivery-1"),
        output_evidence_digests=tuple(sorted((digest("result"), digest("chart")))),
    )
    assert version.attempt == 1
    assert version.input_digest == digest("delivery-0")
    assert version.output_digest == digest("delivery-1")
    assert version.input_evidence_digests == (digest("initial"),)
    assert version.output_evidence_digests == tuple(sorted((digest("result"), digest("chart"))))
    assert controller.current_digest == digest("delivery-1")


def test_two_revisions_then_limit():
    rubric = rubric_for()
    controller = controller_for(rubric)
    evaluation = evaluation_for(rubric, check())

    assert ask(controller, evaluation).allowed
    controller.record(output_digest=digest("delivery-1"))
    assert ask(controller, evaluation).allowed
    controller.record(output_digest=digest("delivery-2"))
    stop = ask(controller, evaluation)

    assert not stop.allowed
    assert stop.reason == "revision_limit_reached"
    assert stop.attempt == 2
    assert controller.report()["revisions"] == 2


def test_pass_and_needs_human_stop_without_consuming():
    rubric = rubric_for()
    controller = controller_for(rubric)
    passed = evaluation_for(rubric, check(outcome="satisfied", code="satisfied"))
    human = evaluation_for(rubric, check(outcome="needs_human", code="human_required"))

    assert ask(controller, passed).reason == "revision_rubric_satisfied"
    assert ask(controller, human).reason == "revision_requires_human"
    assert controller.report()["revisions"] == 0

    assert ask(controller, evaluation_for(rubric, check())).allowed


@pytest.mark.parametrize(
    ("code", "cause"),
    [
        ("no_source", "source"),
        ("no_capability", "capability"),
        ("authorization_denied", "authorization"),
    ],
)
def test_nonrevisionable_cause_does_not_consume(code, cause):
    rubric = rubric_for()
    controller = controller_for(rubric)

    stop = ask(controller, evaluation_for(rubric, check(code=code)))

    assert not stop.allowed
    assert stop.reason == f"revision_not_applicable_{cause}"
    assert controller.report()["revisions"] == 0
    assert ask(controller, evaluation_for(rubric, check())).attempt == 1
    controller.record(output_digest=digest("delivery-1"))
    assert controller.ledger().stopped_reason == "revision_open"


@pytest.mark.parametrize(
    ("budget", "reason"),
    [
        (
            RevisionBudget(remaining_seconds=0, remaining_tokens=1, remaining_tool_calls=1),
            "revision_time_budget_exhausted",
        ),
        (
            RevisionBudget(remaining_seconds=60, remaining_tokens=0, remaining_tool_calls=1),
            "revision_token_budget_exhausted",
        ),
        (
            RevisionBudget(remaining_seconds=60, remaining_tokens=1, remaining_tool_calls=0),
            "revision_tool_budget_exhausted",
        ),
    ],
)
def test_spent_budget_stops_without_consuming(budget, reason):
    rubric = rubric_for()
    controller = controller_for(rubric)

    stop = ask(controller, evaluation_for(rubric, check()), budget=budget)

    assert not stop.allowed
    assert stop.reason == reason
    assert controller.report()["revisions"] == 0


def test_time_budget_wins_when_several_are_spent():
    rubric = rubric_for()
    controller = controller_for(rubric)
    spent = RevisionBudget(remaining_seconds=0, remaining_tokens=0, remaining_tool_calls=0)

    assert ask(controller, evaluation_for(rubric, check()), budget=spent).reason == (
        "revision_time_budget_exhausted"
    )


def test_changed_rubric_is_refused():
    rubric = rubric_for()
    other = rubric_for(task_id=rubric.task_id, metric_version="v2")
    controller = controller_for(rubric)

    with pytest.raises(RevisionRefused, match="revision_rubric_changed"):
        ask(controller, evaluation_for(other, check()))


def test_other_task_evaluation_is_refused():
    rubric = rubric_for()
    controller = controller_for(rubric)

    with pytest.raises(RevisionRefused, match="revision_task_mismatch"):
        ask(controller, evaluation_for(rubric_for(), check()))


def test_input_digest_must_be_current():
    rubric = rubric_for()
    controller = controller_for(rubric)

    with pytest.raises(RevisionRefused, match="revision_input_not_current"):
        ask(controller, evaluation_for(rubric, check()), input_digest=digest("forged"))


def test_input_evidence_must_be_current():
    rubric = rubric_for()
    controller = controller_for(rubric)

    with pytest.raises(RevisionRefused, match="revision_evidence_not_current"):
        ask(
            controller,
            evaluation_for(rubric, check()),
            evidence=(digest("other"),),
        )


def test_output_must_be_recorded_before_another_request():
    rubric = rubric_for()
    controller = controller_for(rubric)
    evaluation = evaluation_for(rubric, check())

    assert ask(controller, evaluation).allowed
    with pytest.raises(RevisionRefused, match="revision_output_not_recorded"):
        ask(controller, evaluation)
    with pytest.raises(RevisionRefused, match="revision_output_not_recorded"):
        controller.ledger()


def test_unchanged_output_is_not_a_revision():
    rubric = rubric_for()
    controller = controller_for(rubric)
    assert ask(controller, evaluation_for(rubric, check())).allowed

    with pytest.raises(RevisionRefused, match="revision_output_unchanged"):
        controller.record(output_digest=controller.current_digest)


def test_record_without_authorization_is_refused():
    rubric = rubric_for()
    controller = controller_for(rubric)

    with pytest.raises(RevisionRefused, match="revision_not_authorized"):
        controller.record(output_digest=digest("delivery-1"))


def test_unknown_or_non_gap_check_is_refused():
    rubric = rubric_for()
    controller = controller_for(rubric)

    with pytest.raises(RevisionRefused, match="revision_check_unknown"):
        ask(controller, evaluation_for(rubric, check()), check_id="gap.other")
    with pytest.raises(RevisionRefused, match="revision_check_not_gap"):
        ask(
            controller,
            evaluation_for(
                rubric,
                check(),
                check(check_id="gap.other", outcome="satisfied", code="satisfied"),
            ),
            check_id="gap.other",
        )


def test_invalid_limits_and_evidence_are_refused():
    rubric = rubric_for()
    with pytest.raises(RevisionRefused, match="revision_limit_invalid"):
        BoundedRevisionController(
            rubric,
            initial_delivery_digest=digest("delivery-0"),
            max_revisions=MAX_REVISIONS + 1,
        )
    controller = controller_for(rubric)
    unordered = tuple(sorted((digest("a"), digest("b")), reverse=True))
    with pytest.raises(RevisionRefused, match="revision_evidence_unordered"):
        ask(controller, evaluation_for(rubric, check()), evidence=unordered)
    with pytest.raises(RevisionRefused, match="revision_evidence_digest_invalid"):
        controller_for(rubric, evidence=("sha256:short",))


def test_ledger_is_open_until_stopped_and_retains_the_chain():
    rubric = rubric_for()
    controller = controller_for(rubric)
    evaluation = evaluation_for(rubric, check())

    assert ask(controller, evaluation).allowed
    first = controller.record(
        output_digest=digest("delivery-1"),
        output_evidence_digests=tuple(sorted((digest("result"),))),
    )
    assert ask(controller, evaluation).allowed
    second = controller.record(
        output_digest=digest("delivery-2"),
        output_evidence_digests=tuple(sorted((digest("result"), digest("chart")))),
    )
    ledger = controller.ledger()

    assert ledger.revision_count == 2
    assert ledger.initial_delivery_digest == digest("delivery-0")
    assert ledger.initial_evidence_digests == (digest("initial"),)
    assert ledger.versions == (first, second)
    assert ledger.stopped_reason == "revision_open"

    stop = ask(controller, evaluation)
    assert stop.reason == "revision_limit_reached"
    closed = controller.ledger()
    assert closed.stopped_reason == "revision_limit_reached"
    assert closed.revision_count == 2


def test_ledger_digest_tamper_is_refused():
    rubric = rubric_for()
    controller = controller_for(rubric)
    ledger = controller.ledger()
    data = ledger.model_dump()
    data["ledger_digest"] = digest("tampered")

    with pytest.raises(ValidationError, match="revision_ledger_digest_mismatch"):
        RevisionLedger(**data)


def test_ledger_chain_tamper_is_refused():
    rubric = rubric_for()
    controller = controller_for(rubric)
    assert ask(controller, evaluation_for(rubric, check())).allowed
    controller.record(output_digest=digest("delivery-1"))
    ledger = controller.ledger()
    broken = ledger.versions[0].model_copy(update={"input_digest": digest("forged")})
    payload = ledger_payload(
        rubric_id=ledger.rubric_id,
        task_id=ledger.task_id,
        rubric_digest=ledger.rubric_digest,
        initial_delivery_digest=ledger.initial_delivery_digest,
        initial_evidence_digests=ledger.initial_evidence_digests,
        versions=(broken,),
        stopped_reason=ledger.stopped_reason,
    )

    with pytest.raises(ValidationError, match="revision_chain_broken"):
        RevisionLedger(
            rubric_id=ledger.rubric_id,
            task_id=ledger.task_id,
            rubric_digest=ledger.rubric_digest,
            initial_delivery_digest=ledger.initial_delivery_digest,
            initial_evidence_digests=ledger.initial_evidence_digests,
            versions=(broken,),
            revision_count=1,
            stopped_reason=ledger.stopped_reason,
            ledger_digest=canonical_hash(payload),
        )


def test_version_cannot_hide_an_unchanged_revision():
    with pytest.raises(ValidationError, match="revision_output_unchanged"):
        RevisionVersion(
            attempt=1,
            failure_code="missing_evidence",
            cause="evidence",
            input_digest=digest("same"),
            output_digest=digest("same"),
        )
