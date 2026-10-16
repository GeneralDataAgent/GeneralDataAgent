"""Run a local, fixed-template Stage 3 classification experiment."""

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ExperimentSpec, SnapshotColumn
from generaldataagent.data_science import DataScienceWorkbench, create_dataset_snapshot


def main() -> None:
    task_id = uuid4()
    rows = [
        {
            "tenure_months": index + 1,
            "monthly_events": (index * 5) % 17,
            "churned": "yes" if index % 4 == 0 else "no",
        }
        for index in range(24)
    ]
    columns = (
        SnapshotColumn(name="tenure_months", data_type="integer", role="feature"),
        SnapshotColumn(name="monthly_events", data_type="integer", role="feature"),
        SnapshotColumn(name="churned", data_type="string", role="target"),
    )
    with tempfile.TemporaryDirectory(prefix="gda-stage3-demo-") as directory:
        store = LocalArtifactStore(Path(directory))
        snapshot = create_dataset_snapshot(
            task_id=task_id,
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
        )
        workbench = DataScienceWorkbench(artifact_store=store)
        report = workbench.run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id="demo-scientist",
            tenant_id="demo-tenant",
            project_id="demo-project",
            data_domain="demo",
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
