from uuid import uuid4

import pytest
from pydantic import ValidationError

from generaldataagent.contracts import (
    AgentRole,
    ArtifactRef,
    ChartSpec,
    DeliveryPackage,
    GroundedClaim,
    TableSpec,
    TaskPlan,
    TaskStep,
    ValidationCheck,
    ValidationResult,
    ValidationStatus,
)


def test_task_plan_accepts_topologically_ordered_steps() -> None:
    plan = TaskPlan(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        objective="Analyze weekly conversion",
        steps=(
            TaskStep(
                step_id="discover",
                objective="Resolve the certified metric",
                agent_role=AgentRole.DISCOVERY,
            ),
            TaskStep(
                step_id="analyze",
                objective="Run read-only analysis",
                agent_role=AgentRole.DATA_ANALYST,
                depends_on=("discover",),
            ),
        ),
    )
    assert len(plan.steps) == 2


def test_task_plan_rejects_forward_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown or forward dependencies"):
        TaskPlan(
            user_id="user-1",
            tenant_id="tenant-1",
            project_id="project-1",
            objective="Invalid plan",
            steps=(
                TaskStep(
                    step_id="analyze",
                    objective="Analyze before discovery",
                    agent_role=AgentRole.DATA_ANALYST,
                    depends_on=("discover",),
                ),
                TaskStep(
                    step_id="discover",
                    objective="Discover data",
                    agent_role=AgentRole.DISCOVERY,
                ),
            ),
        )


def test_validation_cannot_pass_with_failed_check() -> None:
    with pytest.raises(ValidationError, match="cannot pass"):
        ValidationResult(
            task_id=uuid4(),
            status=ValidationStatus.PASS,
            verifier="verifier",
            checks=(
                ValidationCheck(
                    check_id="grounding",
                    passed=False,
                    code="missing_evidence",
                    message="A claim has no evidence.",
                ),
            ),
        )


def test_table_rejects_rows_with_wrong_width() -> None:
    with pytest.raises(ValidationError, match="column count"):
        TableSpec(
            columns=("segment", "metric_value"),
            rows=(("web",),),
            data_artifact_uri="artifact://local/data",
        )


def test_delivery_rejects_claim_evidence_outside_package() -> None:
    task_id = uuid4()
    data_ref = ArtifactRef(
        task_id=task_id,
        uri=f"artifact://local/{task_id}/data.json",
        artifact_type="metric_query_result",
        content_hash=f"sha256:{'0' * 64}",
        created_by="test",
        size_bytes=2,
    )
    table = TableSpec(
        columns=("metric_value",),
        rows=((1,),),
        data_artifact_uri=data_ref.uri,
    )
    with pytest.raises(ValidationError, match="missing evidence"):
        DeliveryPackage(
            metric="orders@v1",
            columns=table.columns,
            rows=table.rows,
            row_count=1,
            table=table,
            chart=ChartSpec(
                chart_type="kpi",
                title="Orders",
                data_artifact_uri=data_ref.uri,
            ),
            summary="One order.",
            claims=(
                GroundedClaim(
                    claim_id="orders",
                    claim_type="fact",
                    text="One order.",
                    evidence_uris=("artifact://local/missing/data.json",),
                ),
            ),
            evidence=(data_ref,),
        )
