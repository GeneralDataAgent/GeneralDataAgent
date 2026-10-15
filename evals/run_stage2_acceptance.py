"""Aggregate local Stage 2 evidence and validated real-environment evidence."""

import argparse
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
from generaldataagent.evaluation import load_external_gate_results, record_external_gate_acceptance
from generaldataagent.policy import ApprovalError, SQLiteApprovalStore

EVAL_DIR = Path(__file__).parent
run_evaluation = run_path(EVAL_DIR / "run_stage2.py")["run_evaluation"]
run_cross_engine_evaluation = run_path(EVAL_DIR / "run_stage2_cross_engine.py")[
    "run_cross_engine_evaluation"
]
run_dbt_adapter_evaluation = run_path(EVAL_DIR / "run_stage2_dbt_adapter.py")[
    "run_dbt_adapter_evaluation"
]
run_development_write_evaluation = run_path(EVAL_DIR / "run_stage2_development_write.py")[
    "run_development_write_evaluation"
]
run_load_evaluation = run_path(EVAL_DIR / "run_stage2_load.py")["run_load_evaluation"]
run_impact_evaluation = run_path(EVAL_DIR / "run_stage2_impact.py")["run_impact_evaluation"]
run_isolation_evaluation = run_path(EVAL_DIR / "run_stage2_isolation.py")[
    "run_isolation_evaluation"
]
run_lineage_evaluation = run_path(EVAL_DIR / "run_stage2_lineage.py")["run_lineage_evaluation"]
run_quality_evaluation = run_path(EVAL_DIR / "run_stage2_quality.py")["run_quality_evaluation"]
run_quality_proposal_evaluation = run_path(EVAL_DIR / "run_stage2_quality_proposals.py")[
    "run_quality_proposal_evaluation"
]
run_schema_evaluation = run_path(EVAL_DIR / "run_stage2_schema.py")["run_schema_evaluation"]
run_transformation_evaluation = run_path(EVAL_DIR / "run_stage2_transformation.py")[
    "run_transformation_evaluation"
]


def run_acceptance_report(
    external_evidence_dir: Path | None = None,
    *,
    now: datetime | None = None,
    trusted_verifiers_path: Path | None = None,
    evidence_ledger_path: Path | None = None,
) -> dict[str, Any]:
    if external_evidence_dir is not None and trusted_verifiers_path is None:
        raise ValueError("external evidence requires a trusted verifier registry")
    if external_evidence_dir is not None and evidence_ledger_path is None:
        raise ValueError("external evidence requires a durable acceptance ledger")
    checked_at = now or datetime.now(UTC)
    component = run_evaluation()
    cross_engine = run_cross_engine_evaluation()
    dbt_adapter = run_dbt_adapter_evaluation()
    development_write = run_development_write_evaluation()
    load = run_load_evaluation()
    impact = run_impact_evaluation()
    isolation = run_isolation_evaluation()
    lineage = run_lineage_evaluation()
    quality = run_quality_evaluation()
    quality_proposals = run_quality_proposal_evaluation()
    schema = run_schema_evaluation()
    transformation = run_transformation_evaluation()
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
            "gate_id": "local_dbt_adapter",
            "status": "passed" if dbt_adapter["failed"] == 0 else "failed",
            "evidence": dbt_adapter,
        },
        {
            "gate_id": "development_write_pilot",
            "status": "passed" if development_write["failed"] == 0 else "failed",
            "evidence": development_write,
        },
        {
            "gate_id": "impact_analysis_matrix",
            "status": "passed" if impact["failed"] == 0 else "failed",
            "evidence": impact,
        },
        {
            "gate_id": "dbt_manifest_lineage_matrix",
            "status": "passed" if lineage["failed"] == 0 else "failed",
            "evidence": lineage,
        },
        {
            "gate_id": "quality_rule_matrix",
            "status": "passed" if quality["failed"] == 0 else "failed",
            "evidence": quality,
        },
        {
            "gate_id": "quality_rule_proposal_safety",
            "status": "passed" if quality_proposals["failed"] == 0 else "failed",
            "evidence": quality_proposals,
        },
        {
            "gate_id": "schema_diff_matrix",
            "status": "passed" if schema["failed"] == 0 else "failed",
            "evidence": schema,
        },
        {
            "gate_id": "transformation_safety_matrix",
            "status": "passed" if transformation["failed"] == 0 else "failed",
            "evidence": transformation,
        },
        {
            "gate_id": "local_parallel_load",
            "status": "passed" if load["passed"] else "failed",
            "evidence": load,
        },
        {
            "gate_id": "parallel_group_isolation",
            "status": "passed" if isolation["passed"] else "failed",
            "evidence": isolation,
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
    external_gates = load_external_gate_results(
        external_evidence_dir,
        now=checked_at,
        trusted_verifiers_path=trusted_verifiers_path,
    )
    external_passed = all(gate["status"] == "passed" for gate in external_gates)
    stage_complete = development_passed and external_passed
    if stage_complete and evidence_ledger_path is not None:
        record_external_gate_acceptance(
            external_gates,
            evidence_ledger_path,
            accepted_at=checked_at,
        )
    return {
        "stage": 2,
        "development_gate_status": "passed" if development_passed else "failed",
        "stage_acceptance_status": "passed" if stage_complete else "incomplete",
        "stage_acceptance_complete": stage_complete,
        "development_gates": development_gates,
        "external_gates": external_gates,
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
        columns=(ColumnSchema(name="id", data_type="INTEGER", nullable=False, ordinal_position=0),),
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-evidence-dir", type=Path)
    parser.add_argument("--trusted-verifiers", type=Path)
    parser.add_argument("--evidence-ledger", type=Path)
    args = parser.parse_args()
    report = run_acceptance_report(
        args.external_evidence_dir,
        trusted_verifiers_path=args.trusted_verifiers,
        evidence_ledger_path=args.evidence_ledger,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.external_evidence_dir is None:
        succeeded = report["development_gate_status"] == "passed"
    else:
        succeeded = report["stage_acceptance_complete"]
    raise SystemExit(0 if succeeded else 1)
