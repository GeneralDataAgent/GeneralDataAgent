import json
from uuid import uuid4

import numpy as np
import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ExperimentSpec, SnapshotColumn, SplitSpec
from generaldataagent.data_science.checks import split_indices, validate_experiment_data
from generaldataagent.data_science.notebook import export_replayed_notebook
from generaldataagent.data_science.sandbox import FixedTemplateSandbox, SandboxError
from generaldataagent.data_science.snapshot import create_dataset_snapshot
from generaldataagent.data_science.workbench import DataScienceWorkbench


@pytest.fixture
def experiment(tmp_path):
    rows = [
        {
            "x": float(i),
            "z": float(i * i % 13),
            "y": 2.0 * i - 3 * (i * i % 13) + 7,
            "group": str(i // 5),
            "time": i,
        }
        for i in range(40)
    ]
    store = LocalArtifactStore(tmp_path / "artifacts")
    snapshot = create_dataset_snapshot(
        task_id=uuid4(),
        tenant_id="t",
        project_id="p",
        data_domain="d",
        source_name="synthetic",
        rows=rows,
        columns=tuple(
            SnapshotColumn(name=name, data_type=kind, role=role)
            for name, kind, role in [
                ("x", "number", "feature"),
                ("z", "number", "feature"),
                ("y", "number", "target"),
                ("group", "string", "group"),
                ("time", "integer", "timestamp"),
            ]
        ),
        artifact_store=store,
    )
    spec = ExperimentSpec(
        task_id=snapshot.task_id,
        snapshot_id=snapshot.snapshot_id,
        problem_type="regression",
        target_column="y",
        feature_columns=("x", "z"),
        baseline="mean",
        estimator="linear_regression",
        primary_metric="r2",
        subgroup_columns=("group",),
        missing_features="train_mean",
    )
    return rows, store, snapshot, spec


@pytest.mark.parametrize("strategy", ["random", "group", "temporal"])
def test_real_multivariate_holdout_against_numpy(experiment, strategy):
    rows, store, snapshot, spec = experiment
    split = SplitSpec(
        strategy=strategy,
        group_column="group" if strategy == "group" else None,
        timestamp_column="time" if strategy == "temporal" else None,
    )
    spec = spec.model_copy(update={"split": split})
    report = DataScienceWorkbench(artifact_store=store).run_experiment(
        snapshot=snapshot, spec=spec, user_id="u", tenant_id="t", project_id="p", data_domain="d"
    )
    assert report.passed and report.template_version == "2.0"
    result = json.loads(store.read_bytes(report.output_artifact))
    model = result["model"]
    assert model["coefficients"] == pytest.approx({"x": 2, "z": -3})
    assert model["intercept"] == pytest.approx(7)
    assert result["metrics"]["r2"] == pytest.approx(1)
    train, test = split_indices(rows, spec)
    independent = np.linalg.lstsq(
        np.array([[1, rows[i]["x"], rows[i]["z"]] for i in train]),
        np.array([rows[i]["y"] for i in train]),
        rcond=None,
    )[0]
    assert list(model["coefficients"].values()) == pytest.approx(independent[1:])
    assert result["statistical_checks"]["split_manifest"]["test_indices"] == test
    assert result["error_analysis"]["baseline_metrics"]["rmse"] > result["metrics"]["rmse"]
    assert max(g.metrics[0].value for g in report.subgroups) < 1e-10


def test_preprocessing_does_not_fit_test_data_and_manifest_validates(experiment):
    rows, _, _, spec = experiment
    train, test = split_indices(rows, spec)
    rows[train[0]]["x"] = None

    def run():
        return FixedTemplateSandbox().run(
            rows=rows, spec=spec, train_indices=train, test_indices=test
        )

    first = run()
    rows[test[0]]["x"] = 1_000_000
    second = run()
    assert first["model"] == second["model"]
    assert first["metrics"] != second["metrics"]
    assert first["model"]["preprocessing"]["imputation_means"]["x"] == pytest.approx(
        np.mean([rows[i]["x"] for i in train if rows[i]["x"] is not None])
    )
    checks = validate_experiment_data(rows, spec, [0, 0, 1], list(range(2, 40)))
    assert checks[0].code == "invalid_split"


def test_rank_missing_and_non_numeric_inputs_reject(experiment):
    rows, _, _, spec = experiment
    train, test = split_indices(rows, spec)
    for row in rows:
        row["z"] = 2 * row["x"]
    with pytest.raises(SandboxError, match="rank deficient"):
        FixedTemplateSandbox().run(rows=rows, spec=spec, train_indices=train, test_indices=test)
    rows[0]["x"] = "bad"
    checks = validate_experiment_data(rows, spec, train, test)
    assert any(c.code == "invalid_numeric_features" for c in checks)


def test_notebook_is_actually_reexecuted_and_modified_code_fails(experiment):
    rows, store, snapshot, spec = experiment
    ref = export_replayed_notebook(snapshot=snapshot, spec=spec, artifact_store=store)
    nb = json.loads(store.read_bytes(ref))
    assert nb["metadata"]["generaldataagent"]["reproducible"]
    assert [c["execution_count"] for c in nb["cells"]] == [1, 2]
    output = json.loads(nb["cells"][-1]["outputs"][0]["text"])
    assert output["metrics"]["r2"] == pytest.approx(1)
    nb["cells"][-1]["source"] += '\n__import__("os").system("echo unsafe")'
    train, test = split_indices(rows, spec)
    with pytest.raises(SandboxError, match="fixed repository template"):
        FixedTemplateSandbox().run(
            rows=rows, spec=spec, train_indices=train, test_indices=test, notebook=nb
        )


def test_notebook_and_workbench_scope_checks(experiment):
    _, store, snapshot, spec = experiment
    with pytest.raises(PermissionError):
        DataScienceWorkbench(artifact_store=store).run_experiment(
            snapshot=snapshot,
            spec=spec,
            user_id="u",
            tenant_id="wrong",
            project_id="p",
            data_domain="d",
        )
    with pytest.raises(ValueError, match="exact task"):
        export_replayed_notebook(
            snapshot=snapshot,
            spec=spec.model_copy(update={"task_id": uuid4()}),
            artifact_store=store,
        )
    tiny = spec.model_copy(update={"feature_columns": ("x", "z")})
    assert not any(
        c.severity == "error" and not c.passed
        for c in validate_experiment_data(
            [{"x": i, "z": i * i, "y": i + 1} for i in range(5)], tiny, [0, 1, 2], [3, 4]
        )
    )
