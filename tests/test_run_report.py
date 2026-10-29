"""One immutable evidence snapshot can feed diagnostics with different purposes."""

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from generaldataagent.evaluation import run_report
from generaldataagent.evaluation.repeat_comparison import compare_repeated_runs
from generaldataagent.evaluation.run_report import ValidatedRunReport
from generaldataagent.evaluation.stage_six_comparison import compare_runs
from generaldataagent.evaluation.stage_six_control import service_readiness
from generaldataagent.evaluation.stage_six_gates import validate_report
from generaldataagent.evaluation.stage_six_stability import canonical_report_hash, repeated_evidence


@pytest.fixture
def legacy():
    return json.loads((Path(__file__).parent / "fixtures/run-report-e19b407.json").read_text())


def diagnostics(baseline, candidate):
    return {
        "comparison": compare_runs(baseline[0], candidate[0]),
        "service": service_readiness(baseline[1]),
        "stability": repeated_evidence(baseline),
        "repeated_comparison": compare_repeated_runs(baseline, candidate, bootstrap_samples=100),
    }


@pytest.mark.parametrize("wrapped", [False, True])
def test_legacy_diagnostics_match_pre_refactor_oracle(legacy, wrapped):
    before = copy.deepcopy(legacy)
    baseline, candidate = legacy["baseline"], legacy["candidate"]
    if wrapped:
        baseline = [ValidatedRunReport(report) for report in baseline]
        candidate = [ValidatedRunReport(report) for report in candidate]
    result = diagnostics(baseline, candidate)
    assert result == legacy["expected"]
    assert legacy == before
    # Service calibration reports telemetry/outage blockers; comparison also
    # reports missing cost evidence without treating it as observed zero cost.
    assert result["service"]["blocked_by"] == [
        "provider_error_rate_exceeded",
        "incomplete_token_telemetry",
    ]
    assert "cost_not_verified" in result["repeated_comparison"]["design_blockers"]
    assert all(not item["production_accepted"] for item in result.values())
    json.dumps(result, allow_nan=False)


def test_source_and_view_mutations_cannot_change_frozen_evidence(legacy):
    baseline = [ValidatedRunReport(report) for report in legacy["baseline"]]
    candidate = [ValidatedRunReport(report) for report in legacy["candidate"]]
    hashes = [run.report_hash for run in baseline + candidate]
    for report in legacy["baseline"] + legacy["candidate"]:
        report["results"].clear()
        report["summary"]["pass_at_1"] = 1
        report["system_fingerprint"].clear()
    view = baseline[0].to_dict()
    view["legacy_annotation"]["values"][2]["a"] = True
    view["results"][0]["passed"] = True
    summary = baseline[0].summary
    summary["difficulty"].clear()
    assert diagnostics(baseline, candidate) == legacy["expected"]
    assert [run.report_hash for run in baseline + candidate] == hashes
    assert baseline[0].to_dict()["legacy_annotation"]["values"][2] == {"a": False}
    with pytest.raises(FrozenInstanceError):
        baseline[0]._json = b"{}"


def test_canonical_hash_and_unknown_fields_are_preserved(legacy):
    report = legacy["baseline"][0]
    before = copy.deepcopy(report)
    run = ValidatedRunReport(report)
    expected = hashlib.sha256(
        json.dumps(report, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    assert (
        run.report_hash == canonical_report_hash(report) == canonical_report_hash(run) == expected
    )
    assert run.to_dict() == before == report
    assert validate_report(run) == validate_report(report) == report["summary"]
    assert ValidatedRunReport.from_report(run) is run


def test_one_snapshot_is_reused_across_all_diagnostic_consumers(legacy, monkeypatch):
    baseline = [ValidatedRunReport(report) for report in legacy["baseline"]]
    candidate = [ValidatedRunReport(report) for report in legacy["candidate"]]

    def fail_if_revalidated(_):
        pytest.fail("an immutable validated report must not be summarized again")

    monkeypatch.setattr(run_report, "summarize", fail_if_revalidated)
    assert diagnostics(baseline, candidate) == legacy["expected"]
    assert validate_report(baseline[0]) == legacy["baseline"][0]["summary"]


def test_raw_report_changes_require_validation_again(legacy):
    report = legacy["baseline"][0]
    snapshot = ValidatedRunReport(report)
    report["summary"]["tokens"] += 1
    assert validate_report(snapshot)["tokens"] == 1000
    for consumer in (
        ValidatedRunReport,
        validate_report,
        service_readiness,
        lambda value: compare_runs(value, legacy["candidate"][0]),
        lambda value: repeated_evidence([value, legacy["baseline"][1]]),
        lambda value: compare_repeated_runs([value], legacy["candidate"], bootstrap_samples=100),
    ):
        with pytest.raises(ValueError, match="summary does not match"):
            consumer(report)


def test_compatibility_summary_is_recomputed_without_normalizing_report(legacy):
    report = legacy["baseline"][0]
    report["summary"]["n"] = 10.0
    run = ValidatedRunReport(report)
    assert type(run.summary["n"]) is int
    assert type(validate_report(report)["n"]) is int
    assert type(run.to_dict()["summary"]["n"]) is float


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_non_json_metadata_is_not_silently_dropped(legacy, value):
    report = legacy["baseline"][0]
    report["unknown_metadata"] = value
    with pytest.raises(ValueError, match="finite JSON evidence"):
        ValidatedRunReport(report)


def test_envelope_requires_a_report_object():
    with pytest.raises(ValueError, match="JSON object"):
        ValidatedRunReport([])
