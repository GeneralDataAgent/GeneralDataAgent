"""Candidate extraction requires a bound, observed repair and keeps review gating."""

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from generaldataagent.business.experience import ExperienceStore
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.experience_extraction import extract_rule_experience
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
NOW = datetime(2026, 10, 24, tzinfo=UTC)
DOCUMENTS = "null means all values.\nfee = fixed_amount + rate * transaction_value / 10000"


@pytest.fixture
def recovered(tmp_path):
    database = tmp_path / "rules.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.execute(
            "INSERT INTO fees VALUES (?)",
            (json.dumps(dict(ID="r", card_scheme="Alpha", fixed_amount="0.05", rate="14")),),
        )
    calls = iter(
        [
            dict(operation="select", semantics_excerpt="fabricated passage"),
            dict(operation="select", semantics_excerpt="null means all values."),
        ]
    )

    class Model:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            return SimpleNamespace(
                content="",
                tool_calls=[dict(name="analyze_fee_rules", args=next(calls))],
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    result = BoundedSQLAgent(Model(), tool_profile="sql-rules", max_steps=2).run(
        Task(
            benchmark="dabstep",
            case_id="synthetic",
            question="Which rules apply?",
            instructions="Answer under answer.",
            difficulty="easy",
            asset=str(database),
        ),
        database,
        documents=DOCUMENTS,
    )
    assert [event["status"] for event in result["trace"]] == ["error", "ok"]
    return result["trace"]


def extract(trace):
    return extract_rule_experience(trace, documents=DOCUMENTS, owner=ACTOR.user_id, created_at=NOW)


def test_real_scripted_tool_repair_produces_only_an_unreviewed_candidate(recovered, tmp_path):
    (candidate,) = extract(recovered)
    assert (candidate.failure_step, candidate.recovery_step) == (1, 2)
    assert candidate.result_hash == recovered[1]["result_hash"]
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    item = candidate.record
    store.capture(item, ACTOR)
    assert store.state(item.experience_id, ACTOR) == "candidate"
    assert store.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == ()
    store.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert store.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == (item,)


@pytest.mark.parametrize(
    "change",
    [
        "old_trace",
        "changed_parameter",
        "wrong_tool",
        "no_success",
        "wrong_code",
        "bad_result_hash",
        "bad_excerpt",
        "bad_checks",
        "incomplete",
        "wrong_steps",
        "skipped_step",
        "intervening_failure",
        "extra_action",
    ],
)
def test_insufficient_or_changed_evidence_produces_no_candidate(recovered, change):
    trace = copy.deepcopy(recovered)
    failure, success = trace
    if change == "old_trace":
        failure.pop("rule_repair_context_hash")
    elif change == "changed_parameter":
        success["action"]["rules"]["empty_list_is_wildcard"] = True
    elif change == "wrong_tool":
        success["tool"] = "sql"
    elif change == "no_success":
        success["status"] = "error"
    elif change == "wrong_code":
        failure["code"] = "rule_month_empty"
    elif change == "bad_result_hash":
        success["result_hash"] = "0" * 64
    elif change == "bad_excerpt":
        success["action"]["rules"]["semantics_excerpt"] = "null but not in documents"
    elif change == "bad_checks":
        success["rule_evidence"]["checks"] = []
    elif change == "incomplete":
        success["rule_evidence"]["complete"] = False
    elif change == "wrong_steps":
        success["step"] = True
    elif change == "skipped_step":
        success["step"] = 3
    elif change == "intervening_failure":
        trace.insert(1, {"step": 1, "status": "repair_refused"})
    else:
        success["action"]["sql"] = "unrelated"
    if change in {"bad_checks", "incomplete"}:
        success["result_hash"] = hashlib.sha256(
            json.dumps(success["rule_evidence"], sort_keys=True, default=str).encode()
        ).hexdigest()
    assert extract(trace) == ()


def test_template_never_copies_trace_text_or_duplicate_lessons(recovered):
    recovered[0]["attempted_sql"] = "SELECT secret FROM private_table"
    recovered[0]["question"] = "expected_answer sentinel"
    recovered[1]["unrelated"] = "reference sql sentinel"
    (candidate,) = extract(recovered + recovered)
    text = candidate.record.model_dump_json()
    assert "sentinel" not in text
    assert "private_table" not in text
    assert "fabricated passage" not in text
    assert candidate.record.does_not_apply_when
    assert "does not justify" in candidate.record.counterexample


def test_missing_documentation_cannot_produce_a_candidate(recovered):
    assert extract_rule_experience(recovered, documents="", owner="u", created_at=NOW) == ()


def cli():
    path = Path(__file__).resolve().parents[1] / "evals/extract_stage10_experience.py"
    spec = importlib.util.spec_from_file_location("extract_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_cli_preserves_provenance_and_review_gate(recovered, tmp_path, monkeypatch):
    module = cli()
    source = (
        Path(__file__).resolve().parents[1]
        / "src/generaldataagent/evaluation/experience_extraction.py"
    )
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(recovered))
    documents = tmp_path / "manual.txt"
    documents.write_text(DOCUMENTS)
    output = tmp_path / "extraction"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(source),
            "--trace",
            str(trace),
            "--documents",
            str(documents),
            "--actor",
            ACTOR.model_dump_json(),
            "--output",
            str(output),
        ],
    )
    module.main()
    report = json.loads((output / "report.json").read_text())
    assert report["candidate_count"] == 1
    assert report["independent_review"] == "not_run"
    assert report["answer_correctness"] == "not_evaluated"
    store = ExperienceStore(output / "candidates.sqlite", clock=lambda: NOW, read_only=True)
    assert (
        store.retrieve(failure_class="rule_semantics_excerpt_missing", tool="rules", actor=ACTOR)
        == ()
    )
    with pytest.raises(SystemExit):
        module.main()


@pytest.mark.parametrize("change", ["replace", "delete"])
def test_cli_hashes_the_exact_input_bytes_used_for_extraction(
    recovered, tmp_path, monkeypatch, change
):
    module = cli()
    trace = tmp_path / "trace.json"
    documents = tmp_path / "manual.txt"
    trace_bytes = json.dumps(recovered, indent=2).encode()
    document_bytes = DOCUMENTS.encode()
    trace.write_bytes(trace_bytes)
    documents.write_bytes(document_bytes)
    original = module.extract_rule_experience

    def extract_then_change(*args, **kwargs):
        candidates = original(*args, **kwargs)
        if change == "replace":
            trace.write_text("[]")
            documents.write_text("different source")
        else:
            trace.unlink()
            documents.unlink()
        return candidates

    monkeypatch.setattr(module, "extract_rule_experience", extract_then_change)
    output = tmp_path / "extraction"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "extract_stage10_experience.py",
            "--trace",
            str(trace),
            "--documents",
            str(documents),
            "--actor",
            ACTOR.model_dump_json(),
            "--output",
            str(output),
        ],
    )
    module.main()
    report = json.loads((output / "report.json").read_text())
    assert report["candidate_count"] == 1
    assert report["trace_hash"] == hashlib.sha256(trace_bytes).hexdigest()
    assert report["documents_hash"] == hashlib.sha256(document_bytes).hexdigest()
    assert report["independent_review"] == "not_run"
