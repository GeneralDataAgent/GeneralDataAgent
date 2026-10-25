"""The workbench and the regression adapter prepare an experiment the same way."""

from uuid import uuid4

from generaldataagent.contracts import ExperimentSpec, SandboxQuota, SplitSpec
from generaldataagent.data_science.experiment import (
    prepare_experiment,
    resolve_sandbox,
    run_prepared_experiment,
)
from generaldataagent.data_science.regression import evaluate_regression

TASK = uuid4()


def rows(count=40):
    return [
        dict(x=float(index), z=float(index % 7), y=float(2 * index + 3), group=f"g{index % 4}")
        for index in range(count)
    ]


def spec(**changes):
    values = (
        dict(
            task_id=TASK,
            snapshot_id=uuid4(),
            problem_type="regression",
            target_column="y",
            feature_columns=("x", "z"),
            baseline="mean",
            estimator="linear_regression",
            primary_metric="rmse",
            split=SplitSpec(strategy="random", test_fraction=0.25),
            random_seed=11,
        )
        | changes
    )
    return ExperimentSpec(**values)


def test_preparation_is_deterministic_and_records_the_exact_rows():
    first = prepare_experiment(rows(), spec())
    second = prepare_experiment(rows(), spec())
    assert first.train == second.train and first.test == second.test
    assert first.rows == tuple(rows())
    assert first.blocked is False


def test_preparation_owns_its_rows_so_a_caller_cannot_edit_the_run():
    source = rows()
    prepared = prepare_experiment(source, spec())
    source[0]["y"] = -1.0
    assert prepared.rows[0]["y"] == 3.0


def test_an_error_check_blocks_without_running_the_worker():
    # Every example is identical, so the train/test boundary must contain one.
    duplicated = [dict(rows()[0]) for _ in range(40)]
    blocked = prepare_experiment(duplicated, spec())
    assert blocked.blocked is True
    assert any(check.code == "cross_split_duplicates" for check in blocked.checks)


def test_reporting_policy_keeps_the_run_unblocked():
    duplicated = [dict(rows()[0]) for _ in range(40)]
    prepared = prepare_experiment(duplicated, spec(duplicate_policy="report"))
    assert prepared.blocked is False
    assert any(check.code == "cross_split_duplicates" for check in prepared.checks)


def test_the_injected_sandbox_is_the_one_that_runs_and_reports_its_fingerprint():
    class Recorder:
        runtime_fingerprint = "injected-runtime"

        def __init__(self):
            self.calls = []

        def run(self, *, rows, spec, train_indices, test_indices, notebook=None):
            self.calls.append((len(rows), spec.experiment_id, list(train_indices), test_indices))
            return {"injected": True}

    sandbox = Recorder()
    prepared = prepare_experiment(rows(), spec())
    result = run_prepared_experiment(prepared, sandbox=sandbox)
    assert result == {"injected": True}
    assert len(sandbox.calls) == 1
    assert sandbox.calls[0][0] == 40
    assert resolve_sandbox(sandbox) is sandbox


def test_regression_binds_the_callers_task_and_snapshot_and_sandbox():
    from generaldataagent.contracts.statistics import RegressionRequest

    class Recorder:
        runtime_fingerprint = "injected-regression-runtime"

        def __init__(self):
            self.specs = []

        def run(self, *, rows, spec, train_indices, test_indices, notebook=None):
            self.specs.append(spec)
            return {"metrics": {"rmse": 0.0}, "subgroups": [], "template_version": "2.2"}

    sandbox = Recorder()
    snapshot = uuid4()
    request = RegressionRequest(target="y", features=["x", "z"], estimator="linear_regression")
    result = evaluate_regression(
        rows(), request, task_id=TASK, snapshot_id=snapshot, sandbox=sandbox
    )
    assert result["runtime_fingerprint"] == "injected-regression-runtime"
    assert len(sandbox.specs) == 1
    assert sandbox.specs[0].task_id == TASK
    assert sandbox.specs[0].snapshot_id == snapshot
    assert sandbox.specs[0].quota == SandboxQuota(memory_mb=256)


def test_regression_without_identity_keeps_its_disposable_task():
    from generaldataagent.contracts.statistics import RegressionRequest

    class Recorder:
        runtime_fingerprint = "injected"

        def __init__(self):
            self.specs = []

        def run(self, *, rows, spec, train_indices, test_indices, notebook=None):
            self.specs.append(spec)
            return {"metrics": {"rmse": 0.0}, "subgroups": [], "template_version": "2.2"}

    sandbox = Recorder()
    request = RegressionRequest(target="y", features=["x", "z"], estimator="linear_regression")
    evaluate_regression(rows(), request, sandbox=sandbox)
    assert sandbox.specs[0].task_id != TASK
    assert sandbox.specs[0].snapshot_id != sandbox.specs[0].task_id
