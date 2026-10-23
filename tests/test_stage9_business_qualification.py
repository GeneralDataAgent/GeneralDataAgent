import json
from fractions import Fraction
from pathlib import Path

import pytest

from generaldataagent.evaluation import rule_business_qualification as qualification
from generaldataagent.evaluation.rule_qualification import independent_context_expected

CORPUS = Path(__file__).resolve().parents[1] / "evals/datasets/stage9-rule-business.json"


def test_authored_language_suite_checks_all_operations_and_refusals(tmp_path):
    output = tmp_path / "business"
    report = qualification.qualify_business(CORPUS, output)
    assert report["n"] == report["processed_n"] == report["passed"] == 18
    assert report["pending_n"] == report["model_calls"] == 0
    assert report["run_status"] == "completed"
    assert report["unseen_evaluation"] == report["independent_human_review"] == "not_run"
    assert report["production_accepted"] is report["benchmark_score"] is False
    assert (output / "corpus.json").read_bytes() == CORPUS.read_bytes()
    rows = {row["case_id"]: row for row in report["results"]}
    assert Fraction(rows["average-credit-25"]["observed_answer"]["average_fee"]) == Fraction(
        "0.1125"
    )
    assert Fraction(rows["counterfactual-month"]["observed_answer"]["delta"]) == Fraction("0.16")
    assert Fraction(rows["counterfactual-day"]["observed_answer"]["delta"]) == Fraction("-0.08")
    assert rows["applicable-day-month-context"]["observed_answer"]["items"] == ["901", "904"]
    with pytest.raises(FileExistsError):
        qualification.qualify_business(CORPUS, output)


def test_governed_business_suite_binds_evidence_and_audit(tmp_path):
    report = qualification.qualify_business(CORPUS, tmp_path / "governed", governed=True)
    assert report["n"] == report["passed"] == 18
    assert report["execution_mode"] == "development_gateway"
    assert report["governance_checked_n"] == report["governance_passed_n"] == 10
    for row in report["results"]:
        if "governance" in row:
            assert all(row["governance"]["checks"].values())
            assert row["governance"]["audit_event_count"] == 2
    assert report["production_accepted"] is False


@pytest.mark.parametrize("fault", ["task", "content", "audit", "checkpoint_content"])
def test_governance_qualification_detects_tampering(tmp_path, monkeypatch, fault):
    from generaldataagent.evaluation.rule_governance_qualification import GovernedRuleRunner

    original = GovernedRuleRunner.invoke

    def corrupt(self, proposal, *, trace_id):
        result = original(self, proposal, trace_id=trace_id)
        ref = result["evidence"]
        if fault == "task":
            ref["task_id"] = "00000000-0000-0000-0000-000000000001"
        elif fault in {"content", "checkpoint_content"}:
            import hashlib

            path = self.artifacts.root / ref["uri"].removeprefix("artifact://local/")
            content = json.loads(path.read_text())
            content["request"]["transaction_value"] = "999"
            payload = json.dumps(content).encode()
            path.write_bytes(payload)
            if fault == "checkpoint_content":
                # A self-consistent new hash still must match the bound checkpoint.
                ref["content_hash"] = "sha256:" + hashlib.sha256(payload).hexdigest()
                ref["size_bytes"] = len(payload)
        else:
            events = [json.loads(line) for line in self.audit.path.read_text().splitlines()]
            events[1]["payload"]["request_fingerprint"] = "wrong"
            self.audit.path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return result

    monkeypatch.setattr(GovernedRuleRunner, "invoke", corrupt)
    report = qualification.qualify_business(CORPUS, tmp_path / "tampered", governed=True)
    assert report["n"] == report["processed_n"] == 18
    assert report["passed"] == 8  # Only the parser refusals remain correct.
    assert report["governance_passed_n"] == 0


def test_context_oracle_honors_empty_lists_year_and_merchant_scope():
    data = json.loads(CORPUS.read_text())
    request = dict(operation="applicable", year=2024, day_of_year=1)
    ids, _ = independent_context_expected(
        data["merchant"], data["payments"], data["annex"], request
    )
    assert ids == ["901"]
    ids, _ = independent_context_expected(
        data["merchant"],
        data["payments"],
        data["annex"],
        request | {"empty_list_is_wildcard": True},
    )
    assert ids == ["901", "904"]
    # March 1 is day 61 in leap year 2024; other years/merchants must not
    # inflate the month total and accidentally satisfy the >300 rule.
    payment = data["payments"][0] | {"day_of_year": 61, "eur_amount": 100}
    payments = [payment, payment | {"year": 2023}, payment | {"merchant": "Other"}]
    ids, _ = independent_context_expected(
        data["merchant"],
        payments,
        data["annex"],
        request | {"day_of_year": 61, "empty_list_is_wildcard": True},
    )
    assert ids == ["901"]


def test_wrong_parse_never_executes_and_keeps_denominator(tmp_path, monkeypatch):
    original = qualification.parse_rule_question

    def wrong(*args, **kwargs):
        request = original(*args, **kwargs)
        return request.model_copy(update={"rules_table": "wrong_table"})

    def forbidden(*args, **kwargs):
        pytest.fail("a mismatched request must never enter the workflow")

    monkeypatch.setattr(qualification, "parse_rule_question", wrong)
    monkeypatch.setattr(qualification.RuleWorkflowRuntime, "run", forbidden)
    report = qualification.qualify_business(CORPUS, tmp_path / "mismatch")
    assert report["n"] == report["processed_n"] == 18
    assert report["passed"] == 8
    assert sum(row["outcome"] == "request_mismatch" for row in report["results"]) == 10


def test_workflow_exceptions_are_persisted_per_case_with_full_plan(tmp_path, monkeypatch):
    output = tmp_path / "faults"
    seen = []

    def broken(*args, **kwargs):
        stored = json.loads((output / "report.json").read_text())
        assert stored["n"] == 18
        assert stored["pending_n"] == 18 - stored["processed_n"]
        seen.append(stored["processed_n"])
        raise RuntimeError("private synthetic details")

    monkeypatch.setattr(qualification.RuleWorkflowRuntime, "run", broken)
    report = qualification.qualify_business(CORPUS, output)
    assert len(seen) == 10
    assert report["n"] == report["processed_n"] == 18
    assert report["passed"] == 8
    assert sum(row["outcome"] == "error" for row in report["results"]) == 10
    assert "private synthetic details" not in (output / "report.json").read_text()


def test_corrupted_numeric_answer_fails_independent_oracle(tmp_path, monkeypatch):
    original = qualification.RuleWorkflowRuntime.run

    def corrupt(self, request, actor):
        thread, state = original(self, request, actor)
        if request.operation == "counterfactual":
            state["delivery"]["delta"] = "999"
        if request.operation == "average_fee":
            state["delivery"]["average_fee"] = "999"
        return thread, state

    monkeypatch.setattr(qualification.RuleWorkflowRuntime, "run", corrupt)
    report = qualification.qualify_business(CORPUS, tmp_path / "corrupted")
    assert report["passed"] == 12
    assert sum(not row["passed"] for row in report["results"]) == 6
