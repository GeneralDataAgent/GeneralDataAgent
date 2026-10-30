"""Skill diagnostics preserve raw evidence and the strict general pairing gate."""

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_bounded_skill_treatment import configuration
from test_stage10_experience_comparison import protocol_hash
from test_stage10_experience_comparison import reports as experience_reports

from generaldataagent.business.extensions import ExtensionAdmission
from generaldataagent.business.skill_bundle import ordered_skill_definitions
from generaldataagent.evaluation.run_report import ValidatedRunReport
from generaldataagent.evaluation.skill_comparison import compare_skill_runs
from generaldataagent.evaluation.skill_treatment import TREATMENT_SOURCE_PATHS, load_skill_treatment
from generaldataagent.evaluation.stage_six import paired_gate, summarize


@pytest.fixture
def evidence(tmp_path):
    path, _, value = configuration(tmp_path)
    treatment = load_skill_treatment(path, root=tmp_path)
    baseline, _ = experience_reports()
    baseline["git_commit"] = "synthetic-commit"
    baseline["protocol"]["steps"] = 6
    protocol_hash(baseline)
    candidate = copy.deepcopy(baseline)
    candidate["results"][0].update(passed=True, failure=None, sub_hits=1)
    candidate["results"][1].update(passed=False, failure="answer_mismatch", sub_hits=0)
    summary = treatment.summary()
    candidate["protocol"]["skill_treatment"] = summary
    candidate["skill_treatment"] = summary
    candidate["system_fingerprint"]["skill_treatment"] = {
        **summary,
        "source_hashes": {name: "c" * 64 for name in TREATMENT_SOURCE_PATHS},
    }
    ordered = ordered_skill_definitions(value.skill, value.dependencies)
    instruction = {
        "mode": "admitted_instructions",
        "references": [
            ["skill", item.name, item.version, ExtensionAdmission.skill_hash(item)]
            for item in ordered
        ],
        "allowed_actions": sorted(value.skill.allowed_tools),
        "rendered_hash": "sha256:" + "b" * 64,
        "model_responses": 2,
        "budget_scope": "declaration_compatibility; existing task meters",
    }
    for row in candidate["results"]:
        row["skill_treatment"] = copy.deepcopy(summary)
        row["skill_extension"] = copy.deepcopy(instruction)
    candidate["summary"] = summarize(candidate["results"])
    protocol_hash(candidate)
    # Offline comparison must not need today's approval state or original files.
    value.admission.registry.path.unlink()
    return baseline, candidate, treatment.payload()


def test_explicit_pair_preserves_reports_snapshots_and_the_general_gate(evidence):
    baseline, candidate, snapshot = evidence
    before = copy.deepcopy(evidence)
    with pytest.raises(ValueError, match="incomparable protocol_hash"):
        paired_gate(baseline, candidate)
    runs = [ValidatedRunReport(report) for report in (baseline, candidate)]
    result = compare_skill_runs(*runs, snapshot)
    assert evidence == before
    assert result["n"] == 4 and result["pass_rate_delta"] == 0
    assert result["improvements"] == ["0"] and result["regressions"] == ["1"]
    assert result["report_hashes"] == [run.report_hash for run in runs]
    assert result["protocol_hashes"] == [baseline["protocol_hash"], candidate["protocol_hash"]]
    assert result["skill_delivery"]["confirmed_cases"] == 4
    assert result["blocked_by"] == []
    assert result["efficiency"]["candidate"]["cost_usd"] is None
    assert result["statistical_inference"] == result["independent_review"] == "not_run"
    assert result["unseen_evaluation"] == result["repeated_pair_qualification"] == "not_run"
    assert result["production_accepted"] is False
    with pytest.raises(ValueError, match="incomparable protocol_hash"):
        paired_gate(baseline, candidate)


@pytest.mark.parametrize(
    "change",
    [
        "budget",
        "agent",
        "route",
        "database",
        "denominator",
        "reorder",
        "duplicate",
        "runner",
        "commit",
        "python",
        "source",
        "difficulty",
        "sub_total",
        "protocol_hash",
        "cohort_file",
        "incomplete",
        "missing_trace",
    ],
)
def test_unlisted_changes_and_missing_evidence_refuse(evidence, change):
    baseline, candidate, snapshot = evidence
    if change == "budget":
        candidate["protocol"]["steps"] = 7
        protocol_hash(candidate)
    elif change == "agent":
        candidate["system_fingerprint"]["agent_hash"] = "different"
    elif change == "route":
        candidate["model_route"] = {"model": "different"}
    elif change == "database":
        candidate["results"][2]["database_hash"] = "different"
    elif change == "denominator":
        for report in (baseline, candidate):
            report["results"].pop()
    elif change == "reorder":
        candidate["results"].reverse()
    elif change == "duplicate":
        candidate["results"][2]["case_id"] = "0"
    elif change in {"runner", "commit", "python", "source"}:
        key = {
            "runner": "runner_hash",
            "commit": "git_commit",
            "python": "python",
            "source": "source_manifest_hash",
        }[change]
        candidate[key] = "different"
    elif change == "difficulty":
        candidate["results"][2]["difficulty"] = "hard"
    elif change == "sub_total":
        candidate["results"][2]["sub_total"] = 2
    elif change == "protocol_hash":
        candidate["protocol_hash"] = "changed"
    elif change == "cohort_file":
        candidate["cohort_file_hash"] = "different"
    elif change == "incomplete":
        candidate["run_status"] = "running"
    else:
        candidate["results"][2].pop("trace")
    if change != "duplicate":
        for report in (baseline, candidate):
            report["summary"] = summarize(report["results"])
    with pytest.raises(ValueError):
        compare_skill_runs(baseline, candidate, snapshot)


@pytest.mark.parametrize(
    "change",
    [
        "snapshot",
        "top",
        "source_inventory",
        "source_hash",
        "references",
        "extra_reference",
        "actions",
        "rendered_hash",
        "boolean_responses",
        "excess_responses",
        "negative_responses",
        "missing_metadata",
    ],
)
def test_treatment_or_delivery_substitution_is_not_accepted(evidence, change):
    baseline, candidate, snapshot = evidence
    instruction = candidate["results"][2]["skill_extension"]
    if change == "snapshot":
        snapshot["body"]["actor"]["user_id"] = "changed"
    elif change == "top":
        candidate["skill_treatment"] = {}
    elif change == "source_inventory":
        candidate["system_fingerprint"]["skill_treatment"]["source_hashes"]["unknown"] = "f" * 64
    elif change == "source_hash":
        candidate["system_fingerprint"]["skill_treatment"]["source_hashes"][
            TREATMENT_SOURCE_PATHS[0]
        ] = "unknown"
    elif change == "references":
        instruction["references"][0][3] = "sha256:" + "f" * 64
    elif change == "extra_reference":
        instruction["references"].append(instruction["references"][0])
    elif change == "actions":
        instruction["allowed_actions"].append("rules")
    elif change == "rendered_hash":
        instruction["rendered_hash"] = "not a hash"
    elif change == "boolean_responses":
        instruction["model_responses"] = True
    elif change == "excess_responses":
        instruction["model_responses"] = 7
    elif change == "negative_responses":
        instruction["model_responses"] = -1
    else:
        candidate["results"][2].pop("skill_extension")
    with pytest.raises(ValueError):
        compare_skill_runs(baseline, candidate, snapshot)


@pytest.mark.parametrize(
    "scope", ["report", "protocol", "system", "row", "instruction", "trace", "failure"]
)
def test_off_arm_skill_leakage_refuses(evidence, scope):
    baseline, candidate, snapshot = evidence
    if scope in {"report", "protocol", "system"}:
        target = {
            "report": baseline,
            "protocol": baseline["protocol"],
            "system": baseline["system_fingerprint"],
        }[scope]
        target["skill_treatment"] = None
        protocol_hash(baseline)
    elif scope == "row":
        baseline["results"][2]["skill_treatment"] = None
    elif scope == "instruction":
        baseline["results"][2]["skill_extension"] = None
    elif scope == "trace":
        baseline["results"][2]["trace"] = [
            {"status": "refused", "code": "skill_action_not_allowed"}
        ]
    else:
        baseline["results"][2]["failure"] = "skill_admission_refused"
        baseline["summary"] = summarize(baseline["results"])
    with pytest.raises(ValueError):
        compare_skill_runs(baseline, candidate, snapshot)


@pytest.mark.parametrize("status", ["unknown_worker_failure", "not_attempted", "unconfirmed"])
def test_unknown_delivery_remains_unknown_and_retains_full_denominator(evidence, status):
    baseline, candidate, snapshot = evidence
    row = candidate["results"][2]
    row.update(tokens=0, usage_status="unknown_worker_failure")
    if status == "unconfirmed":
        row["failure"] = "model_error"
        row["skill_extension"]["model_responses"] = 0
    else:
        row["skill_treatment"]["delivery_status"] = status
        row.pop("skill_extension")
        row["failure"] = (
            "worker_error" if status == "unknown_worker_failure" else "service_circuit_open"
        )
        if status == "not_attempted":
            row["attempted"] = False
    candidate["summary"] = summarize(candidate["results"])
    result = compare_skill_runs(baseline, candidate, snapshot)
    assert result["n"] == 4 and result["skill_delivery"]["confirmed_cases"] == 3
    delivery = result["skill_delivery"]["per_case"][2]
    assert delivery["status"] == status
    assert delivery["model_responses"] == (0 if status == "unconfirmed" else None)
    assert "skill_delivery_unconfirmed" in result["blocked_by"]
    assert "service_failures_confound_attribution" in result["blocked_by"]
    assert "incomplete_token_telemetry" in result["blocked_by"]
    assert result["improvements"] == ["0"] and result["regressions"] == ["1"]


@pytest.mark.parametrize("code", ["skill_admission_refused", "skill_action_not_allowed"])
def test_runtime_refusals_are_visible_with_distinct_attribution_meaning(evidence, code):
    baseline, candidate, snapshot = evidence
    row = candidate["results"][2]
    row["failure"] = code
    row["trace"] = [{"status": "refused", "code": code}]
    candidate["summary"] = summarize(candidate["results"])
    result = compare_skill_runs(baseline, candidate, snapshot)
    key = "admission_refusal_cases" if code == "skill_admission_refused" else "action_refusal_cases"
    assert result["skill_delivery"][key] == ["2"]
    assert ("skill_admission_unavailable_confound_attribution" in result["blocked_by"]) == (
        code == "skill_admission_refused"
    )


def test_a_failed_case_without_a_cause_is_not_labelled_passed(evidence):
    baseline, candidate, snapshot = evidence
    baseline["results"][2]["failure"] = ""
    baseline["summary"] = summarize(baseline["results"])
    result = compare_skill_runs(baseline, candidate, snapshot)
    assert result["failure_transitions"]["unrecorded_failure->answer_mismatch"] == 1


def test_cli_preserves_evidence_and_refuses_to_overwrite(evidence, tmp_path, monkeypatch):
    baseline, candidate, snapshot = evidence
    paths = []
    for name, value in zip(("off", "on", "snapshot"), evidence, strict=True):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        paths.append(path)
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    spec = importlib.util.spec_from_file_location(
        "skill_comparison_cli", Path(__file__).resolve().parents[1] / "evals/compare_skill_runs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    output = tmp_path / "comparison.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_skill_runs.py",
            "--baseline",
            str(paths[0]),
            "--candidate",
            str(paths[1]),
            "--treatment-snapshot",
            str(paths[2]),
            "--output",
            str(output),
        ],
    )
    module.main()
    result = json.loads(output.read_text())
    assert result["n"] == 4 and result["production_accepted"] is False
    assert hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        module.main()
    assert output.read_bytes() == before
