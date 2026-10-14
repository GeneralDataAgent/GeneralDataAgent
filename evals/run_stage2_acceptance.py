"""Aggregate local Stage 2 evidence without claiming external acceptance."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from generaldataagent.contracts import (
    AgentRole,
    ArtifactClassification,
    ColumnSchema,
    RelationSchema,
    RiskLevel,
    ToolRequest,
)
from generaldataagent.data_engineering import SchemaDriftError, require_compatible_schema
from generaldataagent.policy import ApprovalError, SQLiteApprovalStore

EVAL_DIR = Path(__file__).parent
run_evaluation = run_path(EVAL_DIR / "run_stage2.py")["run_evaluation"]
run_cross_engine_evaluation = run_path(EVAL_DIR / "run_stage2_cross_engine.py")[
    "run_cross_engine_evaluation"
]
run_load_evaluation = run_path(EVAL_DIR / "run_stage2_load.py")["run_load_evaluation"]
run_impact_evaluation = run_path(EVAL_DIR / "run_stage2_impact.py")[
    "run_impact_evaluation"
]
run_quality_evaluation = run_path(EVAL_DIR / "run_stage2_quality.py")[
    "run_quality_evaluation"
]


EXTERNAL_GATES = (
    (
        "authoritative_profiler",
        "Compare profiles with an independent authoritative profiler at >=99% agreement.",
    ),
    (
        "real_dbt_project",
        "Qualify dbt compile and selected tests on a real non-production project.",
    ),
    (
        "authoritative_catalog",
        "Verify critical lineage dependencies against an authoritative catalog and Dashboards.",
    ),
    (
        "external_approval_service",
        "Replace the local SQLite approval store with an independently authorized service.",
    ),
    (
        "production_scale_load",
        "Run production-scale capacity, saturation, and recovery tests in the target environment.",
    ),
)


def run_acceptance_report() -> dict[str, Any]:
    component = run_evaluation()
    cross_engine = run_cross_engine_evaluation()
    load = run_load_evaluation()
    impact = run_impact_evaluation()
    quality = run_quality_evaluation()
    schema_gate = _schema_gate_probe()
    approval_gate = _approval_gate_probe()
    development_gates = (
        {
            "gate_id": "frozen_components",
            "status": "passed" if component["failed"] == 0 else "failed",
            "evidence": component,
        },
        {
            "gate_id": "cross_engine_profile_parity",
            "status": "passed" if cross_engine["failed"] == 0 else "failed",
            "evidence": cross_engine,
        },
        {
            "gate_id": "impact_analysis_matrix",
            "status": "passed" if impact["failed"] == 0 else "failed",
            "evidence": impact,
        },
        {
            "gate_id": "quality_rule_matrix",
            "status": "passed" if quality["failed"] == 0 else "failed",
            "evidence": quality,
        },
        {
            "gate_id": "local_parallel_load",
            "status": "passed" if load["passed"] else "failed",
            "evidence": load,
        },
        {
            "gate_id": "schema_drift_fail_closed",
            "status": "passed" if schema_gate["passed"] else "failed",
            "evidence": schema_gate,
        },
        {
            "gate_id": "approval_exactly_once",
            "status": "passed" if approval_gate["passed"] else "failed",
            "evidence": approval_gate,
        },
    )
    development_passed = all(gate["status"] == "passed" for gate in development_gates)
    return {
        "stage": 2,
        "development_gate_status": "passed" if development_passed else "failed",
        "stage_acceptance_status": "incomplete",
        "stage_acceptance_complete": False,
        "development_gates": development_gates,
        "external_gates": tuple(
            {"gate_id": gate_id, "status": "not_run", "required_evidence": requirement}
            for gate_id, requirement in EXTERNAL_GATES
        ),
    }


def _schema_gate_probe() -> dict[str, Any]:
    expected = RelationSchema(
        connector_id="acceptance",
        relation="orders",
        columns=(
            ColumnSchema(name="id", data_type="INTEGER", nullable=False, ordinal_position=0),
            ColumnSchema(name="amount", data_type="REAL", nullable=True, ordinal_position=1),
        ),
    )
    actual = RelationSchema(
        connector_id="acceptance",
        relation="orders",
        columns=(
            ColumnSchema(name="id", data_type="INTEGER", nullable=False, ordinal_position=0),
        ),
    )
    try:
        require_compatible_schema(expected, actual)
    except SchemaDriftError as exc:
        return {
            "passed": exc.diff.breaking,
            "outcome": "replan_required",
            "changes": [item.model_dump(mode="json") for item in exc.diff.changes],
        }
    return {"passed": False, "outcome": "silently_continued", "changes": []}


def _approval_gate_probe() -> dict[str, Any]:
    with TemporaryDirectory(prefix="generaldataagent-approval-gate-") as directory:
        store = SQLiteApprovalStore(Path(directory) / "approvals.sqlite")
        request = ToolRequest(
            task_id=uuid4(),
            user_id="requester",
            tenant_id="acceptance",
            project_id="stage2",
            agent_role=AgentRole.REPORTER,
            tool_name="dataset.export",
            action="export",
            resource="warehouse.acceptance.orders",
            arguments={"limit": 10},
            data_domains=("commerce",),
            requested_risk=RiskLevel.R2,
        )
        approval = store.create_request(
            request,
            impact_scope="Export at most 10 internal acceptance rows.",
            cost_estimate="Less than one minute of local compute.",
            classification=ArtifactClassification.INTERNAL,
            rollback_plan="Delete the generated acceptance artifact.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        store.approve(approval.approval_id, approver_id="reviewer")
        store.consume(approval.approval_id, request)
        restarted = SQLiteApprovalStore(Path(directory) / "approvals.sqlite")
        replay_blocked = False
        try:
            restarted.consume(approval.approval_id, request)
        except ApprovalError as exc:
            replay_blocked = "already been consumed" in str(exc)
        return {
            "passed": replay_blocked,
            "approved_request_bound": True,
            "restart_replay_blocked": replay_blocked,
        }


if __name__ == "__main__":
    report = run_acceptance_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["development_gate_status"] == "passed" else 1)
