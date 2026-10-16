"""Frozen Stage 3 workbench evaluation runner."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import (
    ExperimentSpec,
    SandboxQuota,
    SnapshotColumn,
    SplitSpec,
)
from generaldataagent.contracts.models import ContractModel
from generaldataagent.data_science.sandbox import SandboxError
from generaldataagent.data_science.snapshot import create_dataset_snapshot
from generaldataagent.data_science.workbench import DataScienceWorkbench, ScienceGateError


class StageThreeCase(ContractModel):
    case_id: str = Field(pattern=r"^stage3_[a-z0-9_]+$")
    problem_type: Literal["binary_classification", "regression"]
    split_strategy: Literal["random", "temporal", "group"]
    mutation: Literal[
        "none",
        "target_proxy",
        "duplicate",
        "invalid_target",
        "single_group",
        "metric_misuse",
        "input_quota",
        "task_mismatch",
    ] = "none"
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    expected: Literal["passed", "rejected"]


class StageThreeCaseResult(ContractModel):
    case_id: str
    expected: Literal["passed", "rejected"]
    actual: Literal["passed", "rejected", "unexpected_error"]
    passed: bool
    detail: str


class StageThreeEvaluation(ContractModel):
    dataset_path: str
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    cases: tuple[StageThreeCaseResult, ...]


def load_stage_three_cases(path: Path) -> tuple[StageThreeCase, ...]:
    cases = tuple(
        StageThreeCase.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    )
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Stage 3 evaluation case ids must be unique")
    return cases


def run_stage_three_evaluation(dataset_path: Path, artifact_root: Path) -> StageThreeEvaluation:
    results = tuple(
        _run_case(case, artifact_root / case.case_id)
        for case in load_stage_three_cases(dataset_path)
    )
    passed = sum(result.passed for result in results)
    return StageThreeEvaluation(
        dataset_path=str(dataset_path),
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        success_rate=passed / len(results),
        cases=results,
    )


def _run_case(case: StageThreeCase, root: Path) -> StageThreeCaseResult:
    try:
        _execute_case(case, root)
        actual: Literal["passed", "rejected", "unexpected_error"] = "passed"
        detail = "experiment completed"
    except (ScienceGateError, SandboxError, ValueError) as exc:
        actual = "rejected"
        detail = str(exc)
    except Exception as exc:  # pragma: no cover - preserves case-level diagnostics
        actual = "unexpected_error"
        detail = f"{type(exc).__name__}: {exc}"
    return StageThreeCaseResult(
        case_id=case.case_id,
        expected=case.expected,
        actual=actual,
        passed=actual == case.expected,
        detail=detail,
    )


def _execute_case(case: StageThreeCase, root: Path) -> None:
    task_id = uuid4()
    rows, columns, target, features, baseline, metric = _fixture(case.problem_type)
    if case.mutation == "target_proxy":
        for row in rows:
            row["proxy"] = row[target]
        columns = (*columns, SnapshotColumn(name="proxy", data_type="string", role="feature"))
        rows = [{column.name: row[column.name] for column in columns} for row in rows]
        features = (*features, "proxy")
    elif case.mutation == "duplicate":
        rows[3] = dict(rows[0])
    elif case.mutation == "invalid_target":
        for row in rows:
            row[target] = "only-class" if case.problem_type == "binary_classification" else None
    elif case.mutation == "single_group":
        for row in rows:
            row["entity_group"] = "one-group"
    elif case.mutation == "metric_misuse":
        metric = "rmse" if case.problem_type == "binary_classification" else "accuracy"

    store = LocalArtifactStore(root)
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id="evaluation-tenant",
        project_id="evaluation-project",
        data_domain="evaluation",
        source_name=case.case_id,
        rows=rows,
        columns=columns,
        artifact_store=store,
    )
    split = SplitSpec(
        strategy=case.split_strategy,
        timestamp_column="observed_at" if case.split_strategy == "temporal" else None,
        group_column="entity_group" if case.split_strategy == "group" else None,
    )
    quota = SandboxQuota(max_input_bytes=1024) if case.mutation == "input_quota" else SandboxQuota()
    spec = ExperimentSpec(
        task_id=uuid4() if case.mutation == "task_mismatch" else task_id,
        snapshot_id=snapshot.snapshot_id,
        problem_type=case.problem_type,
        target_column=target,
        feature_columns=features,
        subgroup_columns=("entity_group",),
        baseline=baseline,
        primary_metric=metric,
        split=split,
        random_seed=case.random_seed,
        quota=quota,
    )
    DataScienceWorkbench(artifact_store=store).run_experiment(
        snapshot=snapshot,
        spec=spec,
        user_id="evaluation-scientist",
        tenant_id="evaluation-tenant",
        project_id="evaluation-project",
        data_domain="evaluation",
    )


def _fixture(problem_type: str):
    classification = problem_type == "binary_classification"
    rows = [
        {
            "feature_a": index + 1,
            "feature_b": (index * 11) % 23,
            "entity_group": f"g{index % 5}",
            "observed_at": f"2026-02-{index + 1:02d}",
            "target": ("yes" if index % 3 == 0 else "no")
            if classification
            else float(index * 2 + (index % 4)),
        }
        for index in range(24)
    ]
    columns = (
        SnapshotColumn(name="feature_a", data_type="integer", role="feature"),
        SnapshotColumn(name="feature_b", data_type="integer", role="feature"),
        SnapshotColumn(name="entity_group", data_type="string", role="group"),
        SnapshotColumn(name="observed_at", data_type="string", role="timestamp"),
        SnapshotColumn(
            name="target", data_type="string" if classification else "number", role="target"
        ),
    )
    return (
        rows,
        columns,
        "target",
        ("feature_a", "feature_b"),
        "majority" if classification else "mean",
        "f1" if classification else "rmse",
    )
