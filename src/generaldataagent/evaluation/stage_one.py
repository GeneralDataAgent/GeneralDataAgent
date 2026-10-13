"""Executable offline grader for the Stage 1 certified-metric baseline."""

from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from generaldataagent.catalog import MetricNotFoundError
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.tools import QueryExecutionError


class MetricRuntime(Protocol):
    def run(self, query: MetricQuery, actor: ActorContext, *, thread_id: str) -> dict[str, Any]: ...


class StageOneEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^s1-[0-9]{3}$")
    category: Literal[
        "certified_metric",
        "uncertified_dimension",
        "unauthorized_domain",
        "unknown_metric",
    ]
    expected: Literal["completed", "rejected"]
    query: MetricQuery
    actor_domains: tuple[str, ...] = Field(min_length=1)
    expected_error: str | None = None

    @model_validator(mode="after")
    def rejection_requires_error(self) -> "StageOneEvalCase":
        if self.expected == "rejected" and self.expected_error is None:
            raise ValueError("rejected cases require expected_error")
        if self.expected == "completed" and self.expected_error is not None:
            raise ValueError("completed cases cannot define expected_error")
        return self


class EvaluationCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    expected: str
    actual: str
    error_code: str | None = None


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_size: int
    passed: int
    failed: int
    pass_rate: float
    category_counts: dict[str, int]
    results: tuple[EvaluationCaseResult, ...]


def load_stage_one_cases(path: Path) -> tuple[StageOneEvalCase, ...]:
    cases: list[StageOneEvalCase] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(StageOneEvalCase.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"invalid evaluation case at line {line_number}") from exc
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case ids must be unique")
    return tuple(cases)


def evaluate_stage_one(
    runtime: MetricRuntime,
    cases: Iterable[StageOneEvalCase],
    *,
    user_id: str = "stage1-evaluator",
    tenant_id: str = "stage1",
    project_id: str = "offline-regression",
) -> EvaluationReport:
    materialized = tuple(cases)
    results: list[EvaluationCaseResult] = []
    for case in materialized:
        actor = ActorContext(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            data_domains=case.actor_domains,
        )
        try:
            output = runtime.run(case.query, actor, thread_id=f"eval-{case.case_id}")
            actual = output.get("status", "unknown")
            passed = case.expected == "completed" and actual == "completed"
            results.append(
                EvaluationCaseResult(
                    case_id=case.case_id,
                    passed=passed,
                    expected=case.expected,
                    actual=actual,
                )
            )
        except Exception as exc:
            error_code = _classify_error(exc)
            passed = case.expected == "rejected" and error_code == case.expected_error
            results.append(
                EvaluationCaseResult(
                    case_id=case.case_id,
                    passed=passed,
                    expected=case.expected,
                    actual="rejected",
                    error_code=error_code,
                )
            )
    passed_count = sum(item.passed for item in results)
    return EvaluationReport(
        dataset_size=len(materialized),
        passed=passed_count,
        failed=len(materialized) - passed_count,
        pass_rate=(passed_count / len(materialized)) if materialized else 0.0,
        category_counts=dict(Counter(case.category for case in materialized)),
        results=tuple(results),
    )


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, MetricNotFoundError):
        return "metric_not_found"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, QueryExecutionError):
        return "query_execution_failed"
    if isinstance(exc, ValueError) and "not certified" in str(exc):
        return "uncertified_dimension"
    return f"unexpected_{type(exc).__name__}"
