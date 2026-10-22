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
        ("1,000", "1000", False),
        ("2,0", "20", False),
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


def test_bird_runner_rejects_changed_database_before_loading_model(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "stage6_runner_under_test", Path(__file__).resolve().parents[1] / "evals/run_stage6.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    data = tmp_path / "data/stage6/bird"
    data.mkdir(parents=True)
    (data / "database-manifest.json").write_text(
        json.dumps({"databases": [{"path": "databases/bank/bank.sqlite", "sha256": "wrong_hash"}]})
    )
    db = tmp_path / "databases/bank/bank.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"substituted database")
    c = Case(Task("bird-mini-dev-sqlite", "1", "q", "", "simple", "bank"), {}, "SELECT 1")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "verify_sources", lambda root: {"sources": []})
    monkeypatch.setattr(runner, "load_bird", lambda path: [c])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_stage6.py",
            "--benchmark",
            "bird",
            "--limit",
            "1",
            "--bird-databases",
            str(tmp_path / "databases"),
            "--output",
            str(tmp_path / "output"),
        ],
    )
    with pytest.raises(ValueError, match="pinned official snapshot"):
        runner.main()


def test_repeated_actions_stop_and_final_sql_can_finish_same_query(database):
    task = Task("bird-mini-dev-sqlite", "7", "q", "", "simple", "bank")
    repeated = BoundedSQLAgent(Model(*[{"sql": "SELECT 1"}] * 3)).run(task, database)
    assert repeated["failure"] == "repeated_action"
    finished = BoundedSQLAgent(Model({"sql": "SELECT 1"}, {"final_sql": "SELECT 1"})).run(
        task, database
    )
    assert finished["failure"] is None
    assert finished["final_sql"] == "SELECT 1"


def test_action_schema_errors_are_visible_for_repair(database):
    model = Model(
        {"final_answer": "wrong field"},
        {"sql": "SELECT 2"},
        {"answers": {"mean": "2"}, "evidence_steps": [2]},
    )
    result = BoundedSQLAgent(model).run(case().task, database)
    assert result["failure"] is None
    assert result["trace"][0]["action_keys"] == ["final_answer"]
    assert "allowed_actions" in str(model.messages)


def test_expanded_token_budget_reaches_answer_blind_worker(database, monkeypatch, capsys):
    import io
    import subprocess
    import sys

    import generaldataagent.evaluation.stage_six_worker as worker
    from generaldataagent.evaluation.stage_six import run_isolated

    seen = {}

    def fake_process(command, **kwargs):
        seen.update(json.loads(kwargs["input"]))
        return SimpleNamespace(stdout=json.dumps({"elapsed_seconds": 0, "failure": None}))

    monkeypatch.setattr(subprocess, "run", fake_process)
    run_isolated(
        case("GOLD_SECRET").task, database, app_type="magpie", provider_id="p", max_tokens=48000
    )
    assert seen["max_tokens"] == 48000
    assert "GOLD_SECRET" not in json.dumps(seen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(seen)))
    monkeypatch.setattr(worker, "resolve_evaluation_model", lambda **kwargs: (None, {}))
    monkeypatch.setattr(worker, "build_evaluation_model", lambda *args: None)
    original = worker.BoundedSQLAgent

    def factory(model, **kwargs):
        agent = original(
            Model(
                {"sql": "SELECT AVG(value) AS mean FROM data"},
                {"answers": {"mean": "2"}, "evidence_steps": [1]},
            ),
            **kwargs,
        )
        assert agent.max_tokens == 48000
        return agent

    monkeypatch.setattr(worker, "BoundedSQLAgent", factory)
    worker.main()
    result = json.loads(capsys.readouterr().out)
    assert result["failure"] is None
    assert result["answers"] == {"mean": "2"}


def test_expanded_budget_allows_actual_consumption_above_original_cap(database):
    class LargerUsageModel(Model):
        def invoke(self, messages):
            response = super().invoke(messages)
            response.usage_metadata = {"total_tokens": 15000}
            return response

    def model():
        return LargerUsageModel(
            {"sql": "SELECT AVG(value) AS mean FROM data"},
            {"answers": {"mean": "2"}, "evidence_steps": [1]},
        )

    old = BoundedSQLAgent(model(), max_tokens=24000).run(case().task, database)
    expanded = BoundedSQLAgent(model(), max_tokens=48000).run(case().task, database)
    assert old["failure"] == "token_budget"
    assert expanded["failure"] is None
    assert old["tokens"] == expanded["tokens"] == 30000
    assert expanded["answers"] == {"mean": "2"}
