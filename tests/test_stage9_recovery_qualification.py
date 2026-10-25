import json
from pathlib import Path

import pytest

from generaldataagent.evaluation import rule_recovery_qualification as qualification

CORPUS = Path(__file__).resolve().parents[1] / "evals/datasets/stage9-rule-business.json"


@pytest.mark.parametrize("fault", ["audit", "write"])
def test_governed_recovery_reopens_checkpoint_without_replaying_nodes(tmp_path, fault):
    output = tmp_path / "recovery"
    report = qualification.qualify_recovery(CORPUS, output, fault=fault)
    assert report["fault_mode"] == fault
    assert report["n"] == report["processed_n"] == report["passed"] == 10
    assert report["parent_corpus_n"] == 18
    assert report["pending_n"] == report["model_calls"] == 0
    assert report["run_status"] == "completed"
    assert report["production_accepted"] is report["benchmark_score"] is False
    assert report["unseen_evaluation"] == report["independent_human_review"] == "not_run"
    assert (output / "corpus.json").read_bytes() == CORPUS.read_bytes()
    for row in report["results"]:
        assert row["fault_mode"] == fault
        assert row["initial_failure"] == (
            "_EvidenceAuditOutage" if fault == "audit" else "_EvidenceWriteOutage"
        )
        assert all(row["checks"].values())
        assert set(row["node_counts"].values()) == {1}
    with pytest.raises(FileExistsError):
        qualification.qualify_recovery(CORPUS, output)


@pytest.mark.parametrize("fault", ["audit", "write"])
def test_recovery_errors_keep_planned_denominator_and_partial_progress(
    tmp_path, monkeypatch, fault
):
    output = tmp_path / "faults"
    observed = []

    def broken(*args):
        stored = json.loads((output / "report.json").read_text())
        assert stored["n"] == 10
        observed.append(stored["processed_n"])
        raise RuntimeError("private diagnostic text")

    monkeypatch.setattr(qualification, "_recover_case", broken)
    report = qualification.qualify_recovery(CORPUS, output, fault=fault)
    assert observed == list(range(10))
    assert report["n"] == report["processed_n"] == 10
    assert report["passed"] == 0
    assert all(row["error_type"] == "RuntimeError" for row in report["results"])
    assert "private diagnostic text" not in (output / "report.json").read_text()


@pytest.mark.parametrize("fault", ["audit", "write"])
def test_recovery_qualification_detects_replayed_nodes(tmp_path, monkeypatch, fault):
    original = qualification.GovernedRuleRunner.invoke

    def replay(self, proposal, *, trace_id):
        if trace_id == "recovered":
            from generaldataagent.contracts.rule_tools import RuleAnalysisRequest

            self.runtime.run(RuleAnalysisRequest.model_validate(proposal.arguments), self.actor)
        return original(self, proposal, trace_id=trace_id)

    monkeypatch.setattr(qualification.GovernedRuleRunner, "invoke", replay)
    report = qualification.qualify_recovery(CORPUS, tmp_path / "replayed", fault=fault)
    assert report["passed"] == 0
    assert report["processed_n"] == 10
    assert all(row["checks"]["no_node_replay"] is False for row in report["results"])


def test_write_qualification_detects_missing_outage(tmp_path, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(qualification.patch, "object", lambda *args, **kwargs: nullcontext())
    report = qualification.qualify_recovery(CORPUS, tmp_path / "no-outage", fault="write")
    assert report["n"] == report["processed_n"] == 10
    assert report["passed"] == 0
    assert all(row["checks"]["expected_outage"] is False for row in report["results"])


def test_unknown_fault_does_not_create_output(tmp_path):
    output = tmp_path / "unknown"
    with pytest.raises(ValueError, match="unknown recovery qualification fault"):
        qualification.qualify_recovery(CORPUS, output, fault="unknown")
    assert not output.exists()
