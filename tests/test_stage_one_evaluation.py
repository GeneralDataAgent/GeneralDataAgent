from pathlib import Path

from generaldataagent.catalog import MetricNotFoundError
from generaldataagent.datasets import build_tlc_metric_catalog
from generaldataagent.evaluation import (
    evaluate_semantic_resolution,
    evaluate_stage_one,
    load_semantic_cases,
    load_stage_one_cases,
)
from generaldataagent.orchestration import MetricAnalysisRuntime


class ContractFakeRuntime:
    def run(self, query, actor, *, thread_id):
        if query.metric_version != "v1" or query.metric_name not in {
            "average_charged_amount",
            "average_fare_amount",
            "average_passenger_count",
            "average_trip_distance",
            "total_charged_amount",
            "trip_count",
            "total_fare_amount",
            "total_trip_distance",
        }:
            raise MetricNotFoundError("unknown certified metric")
        if "mobility.tlc" not in actor.data_domains:
            raise PermissionError("unauthorized data domain")
        if set(query.dimensions) - {"pickup_borough"}:
            raise ValueError("dimensions are not certified for this metric")
        return {"status": "completed"}


def dataset_path() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "datasets" / "stage1.jsonl"


def semantic_dataset_path() -> Path:
    return Path(__file__).resolve().parents[1] / "evals" / "datasets" / "stage1-semantic.jsonl"


def test_stage_one_dataset_has_frozen_sixty_case_baseline() -> None:
    cases = load_stage_one_cases(dataset_path())

    assert len(cases) == 60
    assert len({case.case_id for case in cases}) == 60
    assert sum(case.expected == "completed" for case in cases) == 30
    assert sum(case.expected == "rejected" for case in cases) == 30


def test_stage_one_grader_classifies_all_baseline_outcomes() -> None:
    report = evaluate_stage_one(ContractFakeRuntime(), load_stage_one_cases(dataset_path()))

    assert report.dataset_size == 60
    assert report.passed == 60
    assert report.failed == 0
    assert report.pass_rate == 1.0
    assert report.category_counts == {
        "certified_metric": 30,
        "uncertified_dimension": 10,
        "unauthorized_domain": 10,
        "unknown_metric": 10,
    }


def test_semantic_resolution_dataset_meets_accuracy_gate() -> None:
    cases = load_semantic_cases(semantic_dataset_path())
    runtime = object.__new__(MetricAnalysisRuntime)
    runtime.catalog = build_tlc_metric_catalog()
    report = evaluate_semantic_resolution(runtime, cases)

    assert report.dataset_size == 26
    assert report.pass_rate >= 0.95
    assert report.failed == 0
    assert report.category_counts == {"resolved": 16, "ambiguous": 6, "not_found": 4}
