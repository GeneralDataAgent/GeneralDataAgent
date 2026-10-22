"""A complete local synthetic analysis-to-experiment path, with an executed Notebook."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ExperimentSpec, SnapshotColumn
from generaldataagent.contracts.statistics import DerivedFeature, StatisticsRequest
from generaldataagent.data_science.analysis_snapshot import (
    create_analysis_snapshot,
    derive_analysis_snapshot,
    load_analysis_rows,
    promote_analysis_snapshot,
)
from generaldataagent.data_science.notebook import export_replayed_notebook
from generaldataagent.data_science.statistics import compute_statistics
from generaldataagent.data_science.workbench import DataScienceWorkbench


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    store = LocalArtifactStore(args.output / "artifacts")
    rows = [
        {
            "x": float(i),
            "z": float(i * i % 13),
            "w": float(i % 7),
            "outcome": float(2 * i - 3 * (i * i % 13) + 7),
        }
        for i in range(40)
    ]
    source = create_analysis_snapshot(
        task_id=uuid4(),
        tenant_id="demo",
        project_id="demo",
        data_domain="synthetic",
        source_name="deterministic synthetic fixture",
        rows=rows,
        columns=tuple(
            SnapshotColumn(
                name=c, data_type="number", role="ignored" if c == "outcome" else "feature"
            )
            for c in rows[0]
        ),
        artifact_store=store,
    )
    derived = derive_analysis_snapshot(
        snapshot=source,
        artifact_store=store,
        features=[
            DerivedFeature(name="row_average", operation="row_mean", columns=["x", "z", "w"])
        ],
    )
    statistics = compute_statistics(
        load_analysis_rows(derived, store),
        StatisticsRequest(columns=["row_average", "outcome"], operation="pearson"),
    )
    snapshot = promote_analysis_snapshot(
        snapshot=derived, target="outcome", features=["x", "z"], artifact_store=store
    )
    spec = ExperimentSpec(
        task_id=snapshot.task_id,
        snapshot_id=snapshot.snapshot_id,
        target_column="outcome",
        feature_columns=("x", "z"),
        problem_type="regression",
        baseline="mean",
        estimator="sklearn_linear_regression",
        primary_metric="rmse",
        random_seed=42,
    )
    report = DataScienceWorkbench(artifact_store=store).run_experiment(
        snapshot=snapshot,
        spec=spec,
        user_id="local-demo",
        tenant_id="demo",
        project_id="demo",
        data_domain="synthetic",
    )
    notebook = export_replayed_notebook(snapshot=snapshot, spec=spec, artifact_store=store)
    (args.output / "experiment.ipynb").write_bytes(store.read_bytes(notebook))
    summary = {
        "scope": "synthetic demonstration, not real-world predictive evidence",
        "source_snapshot": source.model_dump(mode="json"),
        "derived_snapshot": derived.model_dump(mode="json"),
        "statistics": statistics,
        "experiment_spec": spec.model_dump(mode="json"),
        "experiment_report": report.model_dump(mode="json"),
        "notebook": notebook.model_dump(mode="json"),
        "production_accepted": False,
    }
    (args.output / "results.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "passed": report.passed,
                "metrics": {m.name: m.value for m in report.metrics},
                "notebook": str((args.output / "experiment.ipynb").resolve()),
            }
        )
    )


if __name__ == "__main__":
    main()
