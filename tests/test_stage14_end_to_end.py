"""Stage 14 exit-condition scenarios across rubric, revision and display.

These tests exercise the stage's end-to-end promises with deterministic pieces
only: a missing time range is rejected and repaired within the allowed revision
budget, a mixed source snapshot is rejected, and an authorization denial stops
without consuming a revision. No model, API or publication path participates.
"""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

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
from generaldataagent.contracts.revision import RevisionBudget
from generaldataagent.contracts.rubric import (
    RubricCheckResult,
    RubricEvaluation,
    RubricMethodRequirement,
    RubricSourceRequirement,
    evaluation_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.review.bounded_revision import BoundedRevisionController
from generaldataagent.review.rubric import evaluate_rubric
from generaldataagent.review.rubric_display import render_rubric_display
from generaldataagent.review.rubric_templates import bind_business_template

START = "2026-11-01"
END = "2026-11-02"
METHOD_ID = "metric.certified"
METHOD_VERSION = "v1"


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def evidence(task, name="result", artifact_type="metric_query_result") -> ArtifactRef:
    return ArtifactRef(
        task_id=task,
        uri=f"artifact://local/{task}/{name}.json",
        artifact_type=artifact_type,
        content_hash=digest(name),
        created_by="producer",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        size_bytes=16,
    )


def package_for(task, *, with_time_range: bool) -> DeliveryPackage:
    data = evidence(task)
    chart = evidence(task, "chart", "chart_spec")
    fact = f"Weekly sales for {START} to {END}." if with_time_range else "Weekly sales increased."
    return DeliveryPackage(
        metric="weekly_sales",
        columns=("metric_value",),
        rows=((10,),),
        row_count=1,
        table=TableSpec(columns=("metric_value",), rows=((10,),), data_artifact_uri=data.uri),
        chart=ChartSpec(chart_type="kpi", title="Weekly sales", data_artifact_uri=chart.uri),
        summary="Weekly sales summary.",
        claims=(
            GroundedClaim(
                claim_id="fact",
                claim_type="fact",
                text=fact,
                evidence_uris=(data.uri,),
            ),
            GroundedClaim(
                claim_id="interpretation",
                claim_type="interpretation",
                text="Channel mix shifted.",
                evidence_uris=(data.uri,),
            ),
            GroundedClaim(
                claim_id="limitation",
                claim_type="limitation",
                text="Synthetic local data only.",
                evidence_uris=(data.uri,),
            ),
        ),
        evidence=(data, chart),
    )


def rubric_for(task):
    return bind_business_template(
        template_id="business.weekly_report",
        task_id=task,
        metric_name="weekly_sales",
        metric_version="v1",
        start_time=START,
        end_time=END,
        sources=(
            RubricSourceRequirement(
                source_id="orders",
                content_hash=digest("orders-v1"),
                version="v1",
            ),
        ),
        method=RubricMethodRequirement(method_id=METHOD_ID, method_version=METHOD_VERSION),
    )


def receipt_for(task, package, *, human_items=()) -> ExecutionReceipt:
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
            scope="human_attestation" if human_items else "producer_self_check",
            passed=True,
            check_codes=("result_artifact_readback", *human_items),
            verifier="human-reviewer" if human_items else None,
        ),
    )


def digest_of(package: DeliveryPackage) -> str:
    return canonical_hash(package.model_dump(mode="json"))


def evidence_digests(package: DeliveryPackage) -> tuple[str, ...]:
    return tuple(sorted(ref.content_hash for ref in package.evidence))


def budget() -> RevisionBudget:
    return RevisionBudget(remaining_seconds=60, remaining_tokens=10_000, remaining_tool_calls=5)


def test_missing_time_range_is_repaired_within_the_revision_budget():
    task = uuid4()
    rubric = rubric_for(task)
    initial = package_for(task, with_time_range=False)
    sources = (receipt_for(task, initial).sources[0],)
    initial_receipt = receipt_for(task, initial)
    first = evaluate_rubric(
        rubric=rubric, package=initial, sources=sources, receipt=initial_receipt
    )
    assert first.verdict == "fail"
    gap = next(item for item in first.checks if item.code == "time_range_not_claimed")

    initial_display = render_rubric_display(rubric, first)
    assert initial_display.verdict == "fail"
    assert [item.code for item in initial_display.gaps] == ["time_range_not_claimed"]

    controller = BoundedRevisionController(
        rubric,
        initial_delivery_digest=digest_of(initial),
        initial_evidence_digests=evidence_digests(initial),
    )
    decision = controller.request(
        first,
        check_id=gap.check_id,
        budget=budget(),
        input_digest=digest_of(initial),
        input_evidence_digests=evidence_digests(initial),
    )
    assert decision.allowed and decision.attempt == 1

    corrected = package_for(task, with_time_range=True)
    controller.record(
        output_digest=digest_of(corrected),
        output_evidence_digests=evidence_digests(corrected),
    )
    final_receipt = receipt_for(task, corrected, human_items=("business_owner_signoff",))
    final = evaluate_rubric(
        rubric=rubric, package=corrected, sources=sources, receipt=final_receipt
    )
    assert final.verdict == "pass"

    stop = controller.request(
        final,
        check_id=final.checks[0].check_id,
        budget=budget(),
        input_digest=digest_of(corrected),
        input_evidence_digests=evidence_digests(corrected),
    )
    assert not stop.allowed and stop.reason == "revision_rubric_satisfied"
    ledger = controller.ledger()
    assert ledger.revision_count == 1
    assert ledger.stopped_reason == "revision_rubric_satisfied"

    display = render_rubric_display(rubric, final)
    assert display.verdict == "pass"
    assert display.provenance.human_signoff == "satisfied"
    assert display.provenance.independent_recheck == "not_run"


def test_mixed_source_snapshot_is_rejected_before_revision():
    task = uuid4()
    rubric = rubric_for(task)
    package = package_for(task, with_time_range=True)
    receipt = receipt_for(task, package)
    mixed = (SourceBinding(source_id="orders", content_hash=digest("other"), version="v1"),)

    result = evaluate_rubric(rubric=rubric, package=package, sources=mixed, receipt=receipt)

    assert result.verdict == "fail"
    assert any(item.code == "source_snapshot_mismatch" for item in result.checks)
    display = render_rubric_display(rubric, result)
    assert [item.code for item in display.gaps] == ["source_snapshot_mismatch"]
    assert display.provenance.human_signoff == "required"


def test_authorization_denial_stops_without_consuming_a_revision():
    task = uuid4()
    rubric = rubric_for(task)
    checks = tuple(
        sorted(
            (
                RubricCheckResult(
                    check_id=(
                        "g-human"
                        if verifier_id == "rubric.human_items_acknowledged"
                        else f"a-{index}"
                    ),
                    verifier_id=verifier_id,
                    outcome=(
                        "needs_human"
                        if verifier_id == "rubric.human_items_acknowledged"
                        else "gap"
                        if verifier_id == "rubric.time_range_bound"
                        else "satisfied"
                    ),
                    code=(
                        "authorization_denied"
                        if verifier_id == "rubric.time_range_bound"
                        else "human_attestation_required"
                        if verifier_id == "rubric.human_items_acknowledged"
                        else "satisfied"
                    ),
                    message="synthetic deterministic result for the stop path",
                )
                for index, verifier_id in enumerate(rubric.delivery.verifier_ids)
            ),
            key=lambda item: item.check_id,
        )
    )
    payload = evaluation_payload(
        task_id=task,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.rubric_digest,
        checks=checks,
    )
    evaluation = RubricEvaluation(
        task_id=task,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.rubric_digest,
        verdict="fail",
        checks=checks,
        evaluation_digest=canonical_hash(payload),
    )
    controller = BoundedRevisionController(
        rubric,
        initial_delivery_digest=digest("delivery-0"),
        initial_evidence_digests=(digest("initial"),),
    )

    decision = controller.request(
        evaluation,
        check_id=next(item.check_id for item in checks if item.code == "authorization_denied"),
        budget=budget(),
        input_digest=digest("delivery-0"),
        input_evidence_digests=(digest("initial"),),
    )

    assert not decision.allowed
    assert decision.reason == "revision_not_applicable_authorization"
    assert controller.report()["revisions"] == 0
    assert controller.ledger().revision_count == 0
