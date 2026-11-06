import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from generaldataagent.contracts.delivery import (
    ChartSpec,
    DeliveryPackage,
    GroundedClaim,
    TableSpec,
)
from generaldataagent.contracts.models import ArtifactRef
from generaldataagent.contracts.receipt import (
    ExecutionMethod,
    ExecutionReceipt,
    ObservedUsage,
    ReceiptVerification,
    SourceBinding,
)
from generaldataagent.contracts.rubric import (
    RubricDeliveryRequirement,
    RubricEvaluation,
    RubricMethodRequirement,
    RubricSourceRequirement,
    TaskRubric,
    rubric_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.review.rubric import RubricRefused, evaluate_rubric, registered_verifiers

START = "2026-11-01"
END = "2026-11-02"
METHOD_ID = "metric.certified"
METHOD_VERSION = "v1"
VERIFIER_IDS = (
    "rubric.required_checks_present",
    "rubric.time_range_bound",
    "rubric.metric_version_bound",
    "rubric.sources_single_snapshot",
    "rubric.claims_grounded",
    "rubric.table_and_chart_evidence",
    "rubric.human_items_acknowledged",
)


def digest(value: str):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def task_id():
    return uuid4()


def evidence(task, name="result", artifact_type="metric_query_result"):
    return ArtifactRef(
        task_id=task,
        uri=f"artifact://local/{task}/{name}.json",
        artifact_type=artifact_type,
        content_hash=digest(name),
        created_by="producer",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        size_bytes=16,
    )


def rubric_for(task, *, verifier_ids=None, human_items=()):
    sources = (
        RubricSourceRequirement(source_id="orders", content_hash=digest("orders-v1"), version="v1"),
    )
    method = RubricMethodRequirement(method_id=METHOD_ID, method_version=METHOD_VERSION)
    delivery = RubricDeliveryRequirement(
        required_claim_types=("fact", "limitation"),
        human_confirmation_items=human_items,
        verifier_ids=verifier_ids or VERIFIER_IDS,
    )
    payload = rubric_payload(
        rubric_id="weekly-report",
        task_id=task,
        metric_name="weekly_sales",
        metric_version="v1",
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
        metric_version="v1",
        start_time=START,
        end_time=END,
        sources=sources,
        method=method,
        delivery=delivery,
        rubric_digest=canonical_hash(payload),
    )


def package_for(task, *, time_text=True, claim_types=("fact", "limitation")):
    data = evidence(task)
    chart = evidence(task, "chart", "chart_spec")
    claims = []
    if "fact" in claim_types:
        text = f"Weekly sales for {START} to {END}." if time_text else "Weekly sales."
        claims.append(
            GroundedClaim(
                claim_id="sales",
                claim_type="fact",
                text=text,
                evidence_uris=(data.uri,),
            )
        )
    if "limitation" in claim_types:
        claims.append(
            GroundedClaim(
                claim_id="limit",
                claim_type="limitation",
                text="Synthetic local data only.",
                evidence_uris=(data.uri,),
            )
        )
    return DeliveryPackage(
        metric="weekly_sales",
        columns=("metric_value",),
        rows=((10,),),
        row_count=1,
        table=TableSpec(columns=("metric_value",), rows=((10,),), data_artifact_uri=data.uri),
        chart=ChartSpec(chart_type="kpi", title="Weekly sales", data_artifact_uri=data.uri),
        summary="Weekly sales summary.",
        claims=tuple(claims),
        evidence=(data, chart),
    )


def receipt_for(
    task,
    package,
    *,
    scope="producer_self_check",
    verifier="deterministic",
    check_codes=("result_artifact_readback",),
):
    named = scope in {"human_attestation", "independent_implementation"}
    return ExecutionReceipt(
        task_id=str(task),
        action="metric.analysis",
        actor_id="producer",
        tenant_id="tenant-1",
        project_id="project-1",
        trace_id="trace-1",
        sources=(
            SourceBinding(source_id="orders", content_hash=digest("orders-v1"), version="v1"),
        ),
        method=ExecutionMethod(method_id=METHOD_ID, method_version=METHOD_VERSION),
        result_refs=(package.evidence[0],),
        completeness="complete",
        usage=ObservedUsage(
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            tool_calls=1,
            telemetry_complete=True,
        ),
        verification=ReceiptVerification(
            scope=scope,
            passed=True,
            check_codes=check_codes,
            verifier=verifier if named else None,
        ),
    )


def scenario(*, verifier_ids=None, human_items=(), **kwargs):
    task = task_id()
    rubric = rubric_for(task, verifier_ids=verifier_ids, human_items=human_items)
    package = package_for(task, **kwargs)
    receipt = receipt_for(
        task,
        package,
        scope="human_attestation" if human_items else "producer_self_check",
        verifier="human-reviewer" if human_items else "deterministic",
        check_codes=("result_artifact_readback", *human_items),
    )
    return rubric, package, (receipt.sources[0],), receipt


def test_all_registered_verifiers_are_known():
    assert registered_verifiers() == (
        "rubric.claims_grounded",
        "rubric.human_items_acknowledged",
        "rubric.metric_version_bound",
        "rubric.required_checks_present",
        "rubric.sources_single_snapshot",
        "rubric.table_and_chart_evidence",
        "rubric.time_range_bound",
    )


def test_complete_delivery_passes_with_stable_digest():
    rubric, package, sources, receipt = scenario()
    first = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)
    second = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)

    assert first.verdict == "pass"
    assert all(item.outcome == "satisfied" for item in first.checks)
    assert first.producer_self_check_only is True
    assert first.model_judgement_used is False and first.revision_performed is False
    assert first.evaluation_digest == second.evaluation_digest
    assert len(first.checks) == 7


def test_check_order_is_independent_of_verifier_declaration_order():
    rubric, package, sources, receipt = scenario(verifier_ids=tuple(reversed(VERIFIER_IDS)))
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)

    assert result.verdict == "pass"
    assert [item.check_id for item in result.checks] == sorted(
        item.check_id for item in result.checks
    )


def test_missing_time_range_is_a_gap():
    rubric, package, sources, receipt = scenario(time_text=False)
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)

    assert result.verdict == "fail"
    assert any(item.code == "time_range_not_claimed" for item in result.checks)


def test_mixed_source_snapshot_is_a_gap():
    rubric, package, sources, receipt = scenario()
    changed = (SourceBinding(source_id="orders", content_hash=digest("other"), version="v1"),)
    result = evaluate_rubric(rubric=rubric, package=package, sources=changed, receipt=receipt)

    assert result.verdict == "fail"
    assert any(item.code == "source_snapshot_mismatch" for item in result.checks)


def test_missing_claim_type_is_a_gap():
    rubric, package, sources, receipt = scenario(claim_types=("fact",))
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)

    assert result.verdict == "fail"
    assert any(item.code == "claims_not_grounded" for item in result.checks)


def test_unknown_verifier_is_refused_before_evaluation():
    with pytest.raises(ValueError, match="rubric_unknown_verifier"):
        rubric_for(task_id(), verifier_ids=(*VERIFIER_IDS, "rubric.model_judge"))


def test_incomplete_or_duplicate_verifier_set_is_refused():
    with pytest.raises(ValueError, match="rubric_incomplete_verifier_set"):
        rubric_for(task_id(), verifier_ids=("rubric.required_checks_present",))
    with pytest.raises(ValueError, match="rubric_duplicate_verifier"):
        rubric_for(task_id(), verifier_ids=(*VERIFIER_IDS, VERIFIER_IDS[0]))


def test_human_items_require_named_attestation():
    rubric, package, sources, receipt = scenario(human_items=("business_owner_signoff",))
    weak = receipt.model_copy(
        update={
            "verification": ReceiptVerification(
                scope="producer_self_check",
                passed=True,
                check_codes=("result_artifact_readback",),
            )
        }
    )
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=weak)

    assert result.verdict == "needs_human"
    assert any(item.code == "human_attestation_required" for item in result.checks)


def test_named_human_attestation_can_pass():
    rubric, package, sources, receipt = scenario(human_items=("business_owner_signoff",))
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)

    assert result.verdict == "pass"
    assert any(item.code == "human_attestation_present" for item in result.checks)


def test_human_attestation_must_name_every_confirmation_item():
    rubric, package, sources, receipt = scenario(human_items=("business_owner_signoff",))
    incomplete = receipt.model_copy(
        update={
            "verification": ReceiptVerification(
                scope="human_attestation",
                passed=True,
                check_codes=("result_artifact_readback",),
                verifier="human-reviewer",
            )
        }
    )
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=incomplete)

    assert result.verdict == "needs_human"
    assert any(item.code == "human_attestation_item_missing" for item in result.checks)


def test_cross_task_evidence_is_refused():
    rubric, package, sources, receipt = scenario()
    foreign = evidence(uuid4())
    bad = package.model_copy(update={"evidence": (foreign, *package.evidence[1:])})
    with pytest.raises(RubricRefused, match="evidence_task_mismatch"):
        evaluate_rubric(rubric=rubric, package=bad, sources=sources, receipt=receipt)


def test_conflicting_same_uri_evidence_is_refused():
    rubric, package, sources, receipt = scenario()
    first = package.evidence[0]
    conflicting = first.model_copy(update={"content_hash": digest("different")})
    bad = package.model_copy(update={"evidence": (first, conflicting, package.evidence[1])})
    with pytest.raises(RubricRefused, match="evidence_binding_conflict"):
        evaluate_rubric(rubric=rubric, package=bad, sources=sources, receipt=receipt)


def test_receipt_task_mismatch_is_refused():
    rubric, package, sources, receipt = scenario()
    bad = receipt.model_copy(update={"task_id": str(uuid4())})
    with pytest.raises(RubricRefused, match="receipt_task_mismatch"):
        evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=bad)


def test_rubric_digest_tamper_is_refused():
    rubric, package, sources, receipt = scenario()
    bad = rubric.model_copy(update={"metric_version": "v2"})
    with pytest.raises(RubricRefused, match="rubric_digest_mismatch"):
        evaluate_rubric(rubric=bad, package=package, sources=sources, receipt=receipt)


def test_evaluation_digest_tamper_is_rejected():
    rubric, package, sources, receipt = scenario()
    result = evaluate_rubric(rubric=rubric, package=package, sources=sources, receipt=receipt)
    with pytest.raises(ValueError, match="evaluation_digest_mismatch"):
        RubricEvaluation(**(result.model_dump() | {"evaluation_digest": digest("other")}))


def test_rubric_declared_but_unused_verifier_is_refused():
    rubric, package, sources, receipt = scenario()
    changed_sources = (
        RubricSourceRequirement(
            source_id="orders",
            content_hash=digest("orders-v1"),
            version="v1",
            verifier_id="rubric.metric_version_bound",
        ),
    )
    payload = rubric_payload(
        rubric_id=rubric.rubric_id,
        task_id=rubric.task_id,
        metric_name=rubric.metric_name,
        metric_version=rubric.metric_version,
        start_time=rubric.start_time,
        end_time=rubric.end_time,
        sources=changed_sources,
        method=rubric.method,
        delivery=rubric.delivery,
    )
    bad = rubric.model_copy(
        update={"sources": changed_sources, "rubric_digest": canonical_hash(payload)}
    )
    with pytest.raises(RubricRefused, match="source_verifier_mismatch"):
        evaluate_rubric(rubric=bad, package=package, sources=sources, receipt=receipt)
