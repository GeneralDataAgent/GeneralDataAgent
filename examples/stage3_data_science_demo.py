"""Run a local, fixed-template Stage 3 classification experiment."""

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ExperimentSpec, SnapshotColumn
from generaldataagent.data_science import (
    DataScienceWorkbench,
    ModelRegistrationWorkflow,
    SQLiteExperimentTracker,
    create_dataset_snapshot,
)
from generaldataagent.policy import InMemoryApprovalStore


def main() -> None:
    task_id = uuid4()
    rows = [
        {
            "tenure_months": index + 1,
            "monthly_events": (index * 5) % 17,
            "region": "north" if index % 2 else "south",
            "churned": "yes" if index % 4 == 0 else "no",
        }
        for index in range(24)
    ]
    columns = (
        SnapshotColumn(name="tenure_months", data_type="integer", role="feature"),
        SnapshotColumn(name="monthly_events", data_type="integer", role="feature"),
        SnapshotColumn(name="region", data_type="string", role="group"),
        SnapshotColumn(name="churned", data_type="string", role="target"),
    )
    with tempfile.TemporaryDirectory(prefix="gda-stage3-demo-") as directory:
        store = LocalArtifactStore(Path(directory))
        snapshot = create_dataset_snapshot(
            task_id=task_id,
            tenant_id="demo-tenant",
            project_id="demo-project",
            data_domain="demo",
            source_name="synthetic-churn-demo",
            rows=rows,
            columns=columns,
            artifact_store=store,
        )
        spec = ExperimentSpec(
            task_id=task_id,
            snapshot_id=snapshot.snapshot_id,
            problem_type="binary_classification",
            target_column="churned",
            feature_columns=("tenure_months", "monthly_events"),
            baseline="majority",
            primary_metric="f1",
            random_seed=42,
            subgroup_columns=("region",),
        )
        workbench = DataScienceWorkbench(
            artifact_store=store,
            tracker=SQLiteExperimentTracker(Path(directory) / "experiments.sqlite"),
        )
        report = workbench.run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id="demo-scientist",
            tenant_id="demo-tenant",
            project_id="demo-project",
            data_domain="demo",
        )
        card = workbench.build_model_card(
            snapshot=snapshot,
            report=report,
            model_name="synthetic-churn-baseline",
            model_version="0.1.0",
            intended_use="Offline comparison baseline for the synthetic demo only.",
            owner="demo-data-science",
            out_of_scope_uses=("Customer decisions", "Production deployment"),
            ethical_considerations=("Review regional performance before any real pilot.",),
            limitations=("Synthetic data and a majority baseline are not predictive evidence.",),
            monitoring_requirements=("Re-run after any snapshot change.",),
            subgroup_evaluation="Regional groups are too small for deployment claims.",
        )
        approvals = InMemoryApprovalStore()
        registration = ModelRegistrationWorkflow(approvals=approvals)
        draft, approval = registration.request_review(
            task_id=task_id,
            model_card=card,
            model_artifact=report.output_artifact,
            user_id="demo-scientist",
            tenant_id="demo-tenant",
            project_id="demo-project",
            data_domain="demo",
        )
        approvals.approve(approval.approval_id, approver_id="demo-model-risk-reviewer")
        approved_draft = registration.finalize_approved_draft(
            draft=draft,
            approval_id=approval.approval_id,
            user_id="demo-scientist",
            tenant_id="demo-tenant",
            project_id="demo-project",
            data_domain="demo",
        )
        print(
            json.dumps(
                {
                    "report": report.model_dump(mode="json"),
                    "model_card": card.model_dump(mode="json"),
                    "registration_draft": approved_draft.model_dump(mode="json"),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
