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
    RUBRIC_VERIFIER_IDS,
    RubricMethodRequirement,
    RubricSourceRequirement,
)
from generaldataagent.contracts.rubric_template import (
    BusinessRubricTemplate,
    template_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.review import rubric_templates
from generaldataagent.review.rubric import evaluate_rubric
from generaldataagent.review.rubric_templates import (
    RubricTemplateRefused,
    bind_business_template,
    business_rubric_template,
    registered_templates,
)

START = "2026-11-01"
END = "2026-11-02"
METHOD_ID = "metric.certified"
METHOD_VERSION = "v1"
VERIFIERS = tuple(sorted(RUBRIC_VERIFIER_IDS))


def digest(value: str):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def source(source_id="orders", version="v1", verifier_id="rubric.sources_single_snapshot"):
    return RubricSourceRequirement(
        source_id=source_id,
        content_hash=digest(source_id + version),
        version=version,
        verifier_id=verifier_id,
    )


def method():
    return RubricMethodRequirement(method_id=METHOD_ID, method_version=METHOD_VERSION)


def bind(template_id="business.weekly_report", *, sources=None, extra_claims=(), extra_human=()):
    return bind_business_template(
        template_id=template_id,
        task_id=uuid4(),
        metric_name="weekly_sales",
        metric_version="v1",
        start_time=START,
        end_time=END,
        sources=sources or (source(),),
        method=method(),
        extra_claim_types=extra_claims,
        extra_human_confirmation_items=extra_human,
    )


def evidence(task, name, artifact_type):
    return ArtifactRef(
        task_id=task,
        uri=f"artifact://local/{task}/{name}.json",
        artifact_type=artifact_type,
        content_hash=digest(name),
        created_by="producer",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        size_bytes=16,
    )


def package_for(task):
    data = evidence(task, "result", "metric_query_result")
    chart = evidence(task, "chart", "chart_spec")
    claims = (
        GroundedClaim(
            claim_id="sales",
            claim_type="fact",
            text=f"Weekly sales for {START} to {END}.",
            evidence_uris=(data.uri,),
        ),
        GroundedClaim(
            claim_id="interpretation",
            claim_type="interpretation",
            text="Channel mix shifted.",
            evidence_uris=(data.uri,),
        ),
        GroundedClaim(
            claim_id="limit",
            claim_type="limitation",
            text="Synthetic local data only.",
            evidence_uris=(data.uri,),
        ),
    )
    return DeliveryPackage(
        metric="weekly_sales",
        columns=("metric_value",),
        rows=((10,),),
        row_count=1,
        table=TableSpec(columns=("metric_value",), rows=((10,),), data_artifact_uri=data.uri),
        chart=ChartSpec(chart_type="kpi", title="Weekly sales", data_artifact_uri=chart.uri),
        summary="Weekly sales summary.",
        claims=claims,
        evidence=(data, chart),
    )


def receipt_for(
    task, package, *, scope="producer_self_check", check_codes=("result_artifact_readback",)
):
    return ExecutionReceipt(
        task_id=str(task),
        action="metric.analysis",
        actor_id="producer",
        tenant_id="tenant-1",
        project_id="project-1",
        trace_id="trace-1",
        sources=(SourceBinding(source_id="orders", content_hash=digest("ordersv1"), version="v1"),),
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
            verifier="business-owner" if scope == "human_attestation" else None,
        ),
    )


def test_registered_templates_are_fixed_and_complete():
    assert registered_templates() == (
        "business.anomaly_list",
        "business.channel_change",
        "business.weekly_report",
    )
    for template_id in registered_templates():
        template = business_rubric_template(template_id)
        assert set(template.verifier_ids) == RUBRIC_VERIFIER_IDS
        assert {"fact", "limitation"} <= set(template.required_claim_types)


def test_weekly_report_binding_adds_required_requirements():
    rubric = bind()

    assert rubric.rubric_id.startswith("business.weekly_report.")
    assert rubric.delivery.required_claim_types == (
        "fact",
        "interpretation",
        "limitation",
    )
    assert rubric.delivery.human_confirmation_items == ("business_owner_signoff",)
    assert set(rubric.delivery.verifier_ids) == RUBRIC_VERIFIER_IDS


def test_channel_change_requires_two_sources():
    with pytest.raises(RubricTemplateRefused, match="template_sources_missing"):
        bind("business.channel_change")
    rubric = bind(
        "business.channel_change",
        sources=(source("orders"), source("channels", "v2")),
    )
    assert len(rubric.sources) == 2
    assert rubric.delivery.human_confirmation_items == ("channel_owner_signoff",)


def test_anomaly_list_binds_recommendation_and_owner_signoff():
    rubric = bind("business.anomaly_list")

    assert rubric.delivery.required_claim_types == (
        "fact",
        "recommendation",
        "limitation",
    )
    assert rubric.delivery.human_confirmation_items == ("operations_owner_signoff",)


def test_unknown_template_is_refused():
    with pytest.raises(RubricTemplateRefused, match="template_unknown"):
        bind("business.unknown")


def test_unknown_or_duplicate_extra_claim_is_refused():
    with pytest.raises(RubricTemplateRefused, match="template_unknown_claim_type"):
        bind(extra_claims=("causal",))
    with pytest.raises(RubricTemplateRefused, match="template_duplicate_claim_type"):
        bind(extra_claims=("fact",))
    with pytest.raises(RubricTemplateRefused, match="template_duplicate_claim_type"):
        bind(extra_claims=("recommendation", "recommendation"))


def test_duplicate_human_item_is_refused():
    with pytest.raises(RubricTemplateRefused, match="template_duplicate_human_item"):
        bind(extra_human=("business_owner_signoff",))
    with pytest.raises(RubricTemplateRefused, match="template_duplicate_human_item"):
        bind(extra_human=("legal_signoff", "legal_signoff"))


def test_duplicate_source_and_source_verifier_mismatch_are_refused():
    with pytest.raises(RubricTemplateRefused, match="template_duplicate_source"):
        bind(
            "business.channel_change",
            sources=(source("orders"), source("orders", "v2")),
        )
    with pytest.raises(RubricTemplateRefused, match="template_source_verifier_mismatch"):
        bind(sources=(source(verifier_id="rubric.metric_version_bound"),))


def test_method_verifier_mismatch_is_refused():
    bad = RubricMethodRequirement(
        method_id=METHOD_ID,
        method_version=METHOD_VERSION,
        verifier_id="rubric.sources_single_snapshot",
    )
    with pytest.raises(RubricTemplateRefused, match="template_method_verifier_mismatch"):
        bind_business_template(
            template_id="business.weekly_report",
            task_id=uuid4(),
            metric_name="weekly_sales",
            metric_version="v1",
            start_time=START,
            end_time=END,
            sources=(source(),),
            method=bad,
        )


def test_template_digest_tamper_is_refused():
    payload = template_payload(
        template_id="business.weekly_report",
        template_version="v1",
        min_sources=1,
        required_claim_types=("fact", "limitation"),
        human_confirmation_items=(),
        verifier_ids=VERIFIERS,
    )
    with pytest.raises(ValueError, match="template_digest_mismatch"):
        BusinessRubricTemplate(
            template_id="business.weekly_report",
            template_version="v1",
            min_sources=2,
            required_claim_types=("fact", "limitation"),
            human_confirmation_items=(),
            verifier_ids=VERIFIERS,
            template_digest=canonical_hash(payload),
        )


def test_tampered_registry_template_is_refused(monkeypatch):
    template = business_rubric_template("business.weekly_report")
    tampered = template.model_copy(update={"min_sources": 2})
    monkeypatch.setitem(rubric_templates._TEMPLATES, "business.weekly_report", tampered)
    with pytest.raises(RubricTemplateRefused, match="template_digest_mismatch"):
        bind()


def test_incomplete_verifier_set_is_refused():
    verifiers = VERIFIERS[:-1]
    payload = template_payload(
        template_id="business.weekly_report",
        template_version="v1",
        min_sources=1,
        required_claim_types=("fact", "limitation"),
        human_confirmation_items=(),
        verifier_ids=verifiers,
    )
    with pytest.raises(ValueError, match="template_incomplete_verifier_set"):
        BusinessRubricTemplate(
            template_id="business.weekly_report",
            template_version="v1",
            min_sources=1,
            required_claim_types=("fact", "limitation"),
            human_confirmation_items=(),
            verifier_ids=verifiers,
            template_digest=canonical_hash(payload),
        )


def test_missing_fact_or_limitation_is_refused():
    claims = ("interpretation", "limitation")
    payload = template_payload(
        template_id="business.weekly_report",
        template_version="v1",
        min_sources=1,
        required_claim_types=claims,
        human_confirmation_items=(),
        verifier_ids=VERIFIERS,
    )
    with pytest.raises(ValueError, match="template_claim_types_incomplete"):
        BusinessRubricTemplate(
            template_id="business.weekly_report",
            template_version="v1",
            min_sources=1,
            required_claim_types=claims,
            human_confirmation_items=(),
            verifier_ids=VERIFIERS,
            template_digest=canonical_hash(payload),
        )


def test_bound_template_needs_named_business_signoff():
    rubric = bind()
    package = package_for(rubric.task_id)
    weak = receipt_for(rubric.task_id, package)
    result = evaluate_rubric(
        rubric=rubric,
        package=package,
        sources=weak.sources,
        receipt=weak,
    )
    assert result.verdict == "needs_human"
    assert any(item.code == "human_attestation_required" for item in result.checks)

    signed = receipt_for(
        rubric.task_id,
        package,
        scope="human_attestation",
        check_codes=("result_artifact_readback", "business_owner_signoff"),
    )
    passed = evaluate_rubric(
        rubric=rubric,
        package=package,
        sources=signed.sources,
        receipt=signed,
    )
    assert passed.verdict == "pass"
