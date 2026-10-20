"""Scorer soundness, leakage boundaries, scope controls and release-gate regression tests."""

import hashlib
import json
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import (
    BoundedSQLAgent,
    Case,
    Task,
    build_csv_database,
    equivalent_rows,
    load_bird,
    load_dabstep,
    load_infi,
    paired_gate,
    sample_cases,
    score_answers,
    summarize,
    verify_sources,
    wilson,
)


@pytest.fixture
def database(tmp_path):
    csv = tmp_path / "source.csv"
    csv.write_text("team,value,note\na,1,\na,2,ok\nb,3,ok\n")
    db = tmp_path / "source.sqlite"
    build_csv_database({"data": csv}, db)
    return db


class Model:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.messages = []

    def invoke(self, messages):
        self.messages.extend(messages)
        value = next(self.actions)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=json.dumps(value), usage_metadata={"total_tokens": 10})


def case(expected="2.00"):
    return Case(
        Task("infiagent-dabench", "1", "Mean?", "@mean[value]", "easy", "source.csv"),
        {"mean": expected},
    )


def test_gold_never_reaches_agent_and_real_query_executes(database):
    c = case("SECRET_REFERENCE_VALUE")
    model = Model(
        {"sql": "SELECT AVG(value) AS mean FROM data"},
        {"answers": {"mean": "2.00"}, "evidence_steps": [1]},
    )
    result = BoundedSQLAgent(model).run(c.task, database)
    assert result["failure"] is None
    assert result["trace"][0]["row_count"] == 1
    assert result["answers"] == {"mean": "2.00"}
    assert "SECRET_REFERENCE_VALUE" not in str(model.messages)
    assert set(asdict(c.task)).isdisjoint({"expected", "reference_sql"})


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM data",
        "UPDATE data SET value=0",
        "DROP TABLE data",
        "SELECT * FROM data; DELETE FROM data",
        'ATTACH DATABASE ":memory:" AS other',
        "PRAGMA writable_schema=ON",
        "SELECT * FROM secrets",
        'SELECT load_extension("evil")',
        'SELECT * FROM read_csv("/etc/passwd")',
    ],
)
def test_adversarial_queries_fail_closed(database, sql):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = BoundedSQLAgent(Model({"sql": sql}), max_steps=1).run(case().task, database)
    assert result["failure"]
    assert result["trace"][0]["status"] == "error"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "actions,failure",
    [
        ([{"answers": {"mean": "2"}, "evidence_steps": []}], "missing_evidence"),
        ([{"answers": {"mean": "2"}, "evidence_steps": [1]}], "missing_evidence"),
        ([{"unsupported": "requires ML"}], "unsupported_capability"),
        ([{"sql": "SELECT 1"}], "step_budget"),
    ],
)
def test_incomplete_work_is_not_success(database, actions, failure):
    assert (
        BoundedSQLAgent(Model(*actions), max_steps=1).run(case().task, database)["failure"]
        == failure
    )


def test_budget_counts_consumed_tokens(database):
    result = BoundedSQLAgent(Model({"sql": "SELECT 1"}), max_tokens=5).run(case().task, database)
    assert result["failure"] == "token_budget"
    assert result["tokens"] == 10
    assert not result["trace"]


@pytest.mark.parametrize(
    "actual,expected,passed",
    [
        ("2", "2.00", True),
        ("2.0000001", "2", True),
        ("2.001", "2", False),
        ("2.01", "2", False),
        ("NaN", "2", False),
        ("inf", "2", False),
        ("BLUE", "blue", False),
        ("blue", "blue", True),
        ("the blue", "blue", False),
    ],
)
def test_infi_upstream_scorer(actual, expected, passed):
    assert score_answers(case(expected), {"mean": actual})[0] is passed


def test_missing_subanswers_count_against_denominator():
    c = Case(case().task, {"mean": "2", "count": "3"})
    assert score_answers(c, {"mean": "2"}) == (False, 1, 2)
    assert score_answers(c, {}) == (False, 0, 2)


@pytest.mark.parametrize(
    "actual,expected,ordered,good",
    [
        ([(1,), (1,)], [(1,), (2,)], False, False),
        ([(1,), (1,)], [(1,)], False, False),
        ([(1,), (2,)], [(2,), (1,)], False, True),
        ([(1,), (2,)], [(2,), (1,)], True, False),
        ([(None,)], [(0,)], False, False),
        ([(None,)], [(None,)], False, True),
        ([(1, 2)], [(2, 1)], False, False),
        ([(1.00000001,)], [(1,)], False, True),
    ],
)
def test_result_equivalence(actual, expected, ordered, good):
    assert equivalent_rows(actual, expected, ordered=ordered) is good


def test_sampling_reproducible_and_answer_independent():
    cases = [
        Case(Task("infi", str(i), "q", "", str(i % 3), str(i % 4)), {"a": str(i)})
        for i in range(30)
    ]
    a = sample_cases(cases, 12)
    altered = [Case(c.task, {"a": "changed"}) for c in cases]
    assert [c.task for c in a] == [c.task for c in sample_cases(altered, 12)]
    assert len({c.task.case_id for c in a}) == 12
    assert len(sample_cases(cases, 100)) == 30
    with pytest.raises(ValueError):
        sample_cases(cases, 0)


def test_source_hash_tampering_and_path_escape(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("a\n1\n")
    item = {"path": "data.csv", "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps({"sources": [item]}))
    assert verify_sources(tmp_path)["sources"] == [item]
    p.write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_sources(tmp_path)
    item["path"] = "../outside.csv"
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("outside")
    manifest.write_text(json.dumps({"sources": [item]}))
    with pytest.raises(ValueError, match="invalid"):
        verify_sources(tmp_path)


def test_csv_import_preserves_null_and_duplicate_rows(database):
    with sqlite3.connect(database) as db:
        assert db.execute("select count(*),sum(value),count(note) from data").fetchone() == (
            3,
            6,
            2,
        )
    with pytest.raises(ValueError, match="already exists"):
        build_csv_database({}, database)


def test_summary_includes_failures_ci_and_nearest_rank_p95():
    results = [
        {
            "case_id": str(i),
            "passed": i < 3,
            "difficulty": "hard",
            "elapsed_seconds": i + 1,
            "tokens": 10,
            "failure": None if i < 3 else "unsupported",
            "sub_hits": int(i < 3),
            "sub_total": 1,
        }
        for i in range(4)
    ]
    report = summarize(results)
    assert report["pass_at_1"] == 0.75
    assert report["p95_seconds"] == 4
    assert report["wilson_95"][0] < 0.75 < report["wilson_95"][1]
    assert report["failures"] == {"unsupported": 1}
    assert not report["production_accepted"]
    assert wilson(0, 0) is None
    with pytest.raises(ValueError):
        summarize([])
    with pytest.raises(ValueError):
        summarize(results + results)


def test_paired_release_blocks_regression_and_security():
    before = {
        "cohort_hash": "a",
        "protocol_hash": "b",
        "benchmark": "infi",
        "results": [{"case_id": "1", "passed": True}],
    }
    after = {**before, "results": [{"case_id": "1", "passed": False}]}
    assert not paired_gate(before, after)["passed"]
    assert not paired_gate(
        before, {**before, "results": [{"case_id": "1", "passed": True, "severe_incident": True}]}
    )["passed"]
    with pytest.raises(ValueError):
        paired_gate(before, {**after, "cohort_hash": "changed"})
    with pytest.raises(ValueError):
        paired_gate(before, {**after, "results": []})


def test_adapters_keep_official_ids_and_gold_separate(tmp_path):
    (tmp_path / "da-dev-questions.jsonl").write_text(
        json.dumps(dict(id=7, question="mean?", file_name="source.csv", level="easy")) + "\n"
    )
    (tmp_path / "da-dev-labels.jsonl").write_text(
        json.dumps(dict(id=7, common_answers=[["mean", "2"]])) + "\n"
    )
    assert load_infi(tmp_path)[0].expected == {"mean": "2"}
    (tmp_path / "dev.jsonl").write_text(
        json.dumps(dict(task_id="5", question="q", guidelines="g", level="easy", answer="NL"))
        + "\n"
    )
    assert load_dabstep(tmp_path)[0].task.case_id == "5"
    bird = tmp_path / "bird.json"
    bird.write_text(
        json.dumps(
            [
                dict(
                    question_id=8,
                    question="q",
                    evidence="e",
                    difficulty="simple",
                    db_id="bank",
                    SQL="SELECT 1",
                )
            ]
        )
    )
    c = load_bird(bird)[0]
    assert c.reference_sql == "SELECT 1"
    assert "SELECT 1" not in str(asdict(c.task))


def test_isolated_worker_deadline_never_receives_golden_answers(database, monkeypatch):
    import subprocess

    from generaldataagent.evaluation.stage_six import run_isolated

    seen = {}

    def timeout(command, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setenv("LANGSMITH_API_KEY", "PRIVATE_TEST_KEY")
    result = run_isolated(case("GOLD_SECRET").task, database, app_type="claude", provider_id="p")
    assert result["failure"] == "worker_deadline"
    assert result["usage_status"] == "unknown_worker_failure"
    assert "GOLD_SECRET" not in seen["input"]
    assert "LANGSMITH_API_KEY" not in seen["env"]
    assert seen["env"]["LANGSMITH_TRACING"] == "false"


def test_provider_errors_are_not_retried_and_do_not_leak_secrets(database):
    result = BoundedSQLAgent(Model(RuntimeError("PRIVATE_TEST_KEY"))).run(case().task, database)
    assert result["failure"] == "model_error"
    assert result["usage_status"] == "partial_or_unknown"
    assert len(result["trace"]) == 1
    assert "PRIVATE_TEST_KEY" not in str(result)


def test_bird_requires_sql_and_rejects_narrative_before_retry(database):
    model = Model(
        {"answers": {"answer": "2"}, "evidence_steps": [1]},
        {"final_sql": "SELECT AVG(value) FROM data"},
    )
    task = Task("bird-mini-dev-sqlite", "7", "Average value?", "", "simple", "bank")
    result = BoundedSQLAgent(model).run(task, database)
    assert result["final_sql"] == "SELECT AVG(value) FROM data"
    assert result["failure"] is None
    assert '"required_output": "final_sql"' in str(model.messages)
