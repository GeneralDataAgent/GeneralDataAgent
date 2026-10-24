"""Rule qualification reports must identify the workflow implementation they test."""

import hashlib
from pathlib import Path

import pytest

from generaldataagent.evaluation.rule_business_qualification import qualify_business
from generaldataagent.evaluation.rule_qualification import qualify_context, qualify_rules
from generaldataagent.evaluation.rule_recovery_qualification import qualify_recovery

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals/datasets/stage9-rule-business.json"
PACKAGE = ROOT / "src/generaldataagent"
COMMON = {
    "business/rules/workflow.py",
    "business/rules/engine.py",
    "business/rules/payments.py",
    "contracts/rules.py",
    "contracts/rule_tools.py",
    "orchestration/rule_workflow.py",
    "orchestration/checkpoints.py",
}
GOVERNED = {
    "business/rules/governed.py",
    "tools/gateway.py",
    "policy/engine.py",
    "policy/approvals.py",
    "artifacts/local.py",
    "observability/audit.py",
}


@pytest.mark.parametrize("suite", ["facets", "context", "business", "governed", "audit", "write"])
def test_qualification_reports_hash_the_executed_workflow_sources(tmp_path, suite):
    output = tmp_path / suite
    if suite == "facets":
        report = qualify_rules(output, count=2)
    elif suite == "context":
        report = qualify_context(output, count=2)
    elif suite in {"business", "governed"}:
        report = qualify_business(CORPUS, output, governed=suite == "governed")
    else:
        report = qualify_recovery(CORPUS, output, fault=suite)
    sources = report["workflow_source_sha256"]
    assert sources.keys() >= COMMON
    assert (sources.keys() >= GOVERNED) is (suite in {"governed", "audit", "write"})
    for name, digest in sources.items():
        assert digest == hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest()
    assert report["n"] == len(report["results"])
    assert report["model_calls"] == 0
    assert report["production_accepted"] is False


@pytest.mark.parametrize("governed", [False, True])
def test_workflow_source_change_is_visible_without_changing_other_hashes(monkeypatch, governed):
    from generaldataagent.evaluation.rule_fingerprint import rule_workflow_fingerprint

    before = rule_workflow_fingerprint(governed=governed)
    original = Path.read_bytes

    def changed(path):
        content = original(path)
        return (
            content + b"\n# synthetic change\n"
            if path == PACKAGE / "business/rules/workflow.py"
            else content
        )

    monkeypatch.setattr(Path, "read_bytes", changed)
    after = rule_workflow_fingerprint(governed=governed)
    assert {key for key in before if before[key] != after[key]} == {"business/rules/workflow.py"}


@pytest.mark.parametrize("suite", ["facets", "context", "business", "recovery"])
def test_missing_source_fails_before_creating_qualification_output(tmp_path, monkeypatch, suite):
    original = Path.read_bytes

    def missing(path):
        if path == PACKAGE / "business/rules/workflow.py":
            raise FileNotFoundError("synthetic missing source")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", missing)
    output = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="synthetic missing source"):
        if suite == "facets":
            qualify_rules(output, count=1)
        elif suite == "context":
            qualify_context(output, count=1)
        elif suite == "business":
            qualify_business(CORPUS, output)
        else:
            qualify_recovery(CORPUS, output)
    assert not output.exists()
