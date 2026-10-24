"""Runner manifests must describe the profiles actually selected for execution."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import Case, Task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_manifest(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "routing_fingerprint_runner", ROOT / "evals/run_stage6.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "verify_sources", lambda root: {"sources": []})
    monkeypatch.setattr(
        runner,
        "resolve_evaluation_model",
        lambda **kwargs: (
            SimpleNamespace(model="scripted", provider_id="scripted"),
            {"route_hash": "scripted"},
        ),
    )

    def build(tables, database):
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE payments (value INT)")
            db.execute("INSERT INTO payments VALUES (1)")

    monkeypatch.setattr(runner, "build_csv_database", build)
    monkeypatch.setattr(runner, "add_json_tables", lambda *args: None)
    executed_profiles = []

    def execute(*args, **kwargs):
        executed_profiles.append(kwargs["tool_profile"])
        return dict(
            answers={"answer": "1"},
            final_sql=None,
            failure=None,
            trace=[],
            tokens=0,
            elapsed_seconds=0.0,
            usage_status="complete",
            cost_usd=0.0,
        )

    monkeypatch.setattr(runner, "run_isolated", execute)
    sequence = 0

    def run(
        questions, *, routing=True, profile="sql", experience_actor=None, experience_database=None
    ):
        nonlocal sequence
        sequence += 1
        cases = [
            Case(Task("dabstep-dev", str(i), q, "", "easy", "payments.csv"), {"answer": "1"})
            for i, q in enumerate(questions)
        ]
        monkeypatch.setattr(runner, "load_dabstep", lambda path: cases)
        output = tmp_path / f"run-{sequence}"
        arguments = [
            "run_stage6.py",
            "--benchmark",
            "dabstep",
            "--limit",
            str(len(cases)),
            "--evaluation-protocol",
            "stage9-v1",
            "--tool-profile",
            profile,
            "--output",
            str(output),
        ]
        if routing:
            arguments.append("--routing")
        if experience_actor is not None:
            experience_database = experience_database or tmp_path / "experience.sqlite"
            if not experience_database.exists():
                with sqlite3.connect(experience_database) as db:
                    db.execute("CREATE TABLE placeholder (value TEXT)")
            arguments.extend(
                [
                    "--experience-database",
                    str(experience_database),
                    "--experience-actor",
                    json.dumps(experience_actor),
                ]
            )
        monkeypatch.setattr(sys, "argv", arguments)
        runner.main()
        report = json.loads((output / "report.json").read_text())
        return report, executed_profiles[-len(cases) :]

    return runner, run


@pytest.mark.parametrize(
    "questions,profiles,stages",
    [
        (["What is the fee?"], ["sql-rules"], {7, 9}),
        (["Find the median amount"], ["sql-science"], {7, 8}),
        (["How many payments?"], ["sql-repair"], set()),
        (
            ["What is the fee?", "Find the median amount", "How many payments?"],
            ["sql-rules", "sql-science", "sql-repair"],
            {7, 8, 9},
        ),
    ],
)
def test_routed_fingerprint_covers_executed_profiles(run_manifest, questions, profiles, stages):
    _, run = run_manifest
    report, executed = run(questions)
    assert sorted(executed) == sorted(profiles)
    fingerprint = report["system_fingerprint"]
    for stage in (7, 8, 9):
        assert bool(fingerprint[f"stage{stage}_sources"]) is (stage in stages)
    assert bool(fingerprint["experience_extraction_hash"]) is (9 in stages)
    assert report["summary"]["n"] == len(questions)
    assert report["summary"]["tokens"] == 0
    assert report["production_accepted"] is False


@pytest.mark.parametrize("profile,stages", [("sql", set()), ("sql-rules", {7, 9})])
def test_fixed_profile_fingerprint_stays_scoped(run_manifest, profile, stages):
    _, run = run_manifest
    report, executed = run(["How many payments?"], routing=False, profile=profile)
    assert executed == [profile]
    for stage in (7, 8, 9):
        assert bool(report["system_fingerprint"][f"stage{stage}_sources"]) is (stage in stages)


@pytest.mark.parametrize(
    "filename,key",
    [("repair_budget.py", "repair_budget_hash"), ("routing.py", "retry_policy_hash")],
)
def test_fixed_profile_fingerprint_detects_retry_source_changes(
    run_manifest, monkeypatch, filename, key
):
    runner, run = run_manifest
    before, _ = run(["How many payments?"], routing=False, profile="sql-repair")
    original = runner.digest

    def changed(path):
        return "changed-source" if path.name == filename else original(path)

    monkeypatch.setattr(runner, "digest", changed)
    after, _ = run(["How many payments?"], routing=False, profile="sql-repair")
    assert before["protocol_hash"] == after["protocol_hash"]
    assert before["cohort_hash"] == after["cohort_hash"]
    assert before["system_fingerprint"][key] != after["system_fingerprint"][key]


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "project_id", "data_domains"])
def test_experience_scope_change_makes_runs_incomparable(run_manifest, field):
    from generaldataagent.evaluation.stage_six_stability import repeated_evidence

    _, run = run_manifest
    actor = dict(
        user_id="scope-user",
        tenant_id="scope-tenant",
        project_id="scope-project",
        data_domains=["payments"],
    )
    before, _ = run(["What is the fee?"], experience_actor=actor)
    changed = actor | {field: ["sales"] if field == "data_domains" else "outside-scope"}
    after, _ = run(["What is the fee?"], experience_actor=changed)
    assert before["protocol"]["experience_store_hash"] == after["protocol"]["experience_store_hash"]
    assert before["protocol_hash"] != after["protocol_hash"]
    assert before["protocol"]["experience_scope_hash"] != after["protocol"]["experience_scope_hash"]
    with pytest.raises(ValueError, match="incomparable repeated-run protocol_hash"):
        repeated_evidence([before, after])


def test_experience_scope_hash_uses_the_same_domain_set_semantics_as_retrieval(run_manifest):
    from generaldataagent.business.actions import content_hash
    from generaldataagent.business.experience import ExperienceStore
    from generaldataagent.contracts import ActorContext

    _, run = run_manifest
    actor = dict(
        user_id="scope-user",
        tenant_id="scope-tenant",
        project_id="scope-project",
        data_domains=["sales", "payments", "payments"],
    )
    before, _ = run(["What is the fee?"], experience_actor=actor)
    after, _ = run(
        ["What is the fee?"], experience_actor=actor | {"data_domains": ["payments", "sales"]}
    )
    expected = ExperienceStore._scope(ActorContext.model_validate(actor))
    assert before["protocol"]["experience_scope_hash"] == expected
    assert before["protocol_hash"] == after["protocol_hash"]
    assert before["system_fingerprint"]["experience"]["scope_hash"] == expected
    assert content_hash(actor) != expected
    rendered = json.dumps(before)
    assert "scope-user" not in rendered
    assert "scope-tenant" not in rendered
    assert "scope-project" not in rendered


@pytest.mark.parametrize("wal", [False, True])
def test_experience_snapshot_survives_source_changes(run_manifest, tmp_path, monkeypatch, wal):
    runner, run = run_manifest
    source = tmp_path / "live.sqlite"
    observed = []
    original = runner.run_isolated
    with sqlite3.connect(source) as live:
        if wal:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE lessons (value TEXT)")
        live.execute("INSERT INTO lessons VALUES ('reviewed-before-run')")
        live.commit()

        def execute(*args, **kwargs):
            path = Path(kwargs["experience_database"])
            observed.append(path)
            assert path != source
            assert path.is_relative_to(tmp_path)
            with sqlite3.connect(path) as frozen:
                assert frozen.execute("SELECT value FROM lessons").fetchall() == [
                    ("reviewed-before-run",)
                ]
            with sqlite3.connect(source) as writer:
                writer.execute("INSERT INTO lessons VALUES ('added-during-run')")
            return original(*args, **kwargs)

        monkeypatch.setattr(runner, "run_isolated", execute)
        actor = dict(user_id="u", tenant_id="t", project_id="p", data_domains=["payments"])
        report, _ = run(
            ["What is the fee?", "How many payments?"],
            experience_actor=actor,
            experience_database=source,
        )
        assert len(observed) == 2
        assert observed[0] == observed[1]
        assert report["protocol"]["experience_store_hash"] == runner.digest(observed[0])
        assert report["summary"]["n"] == 2
        assert report["summary"]["tokens"] == 0


def test_runner_reports_support_explicit_experience_treatment_comparison(run_manifest):
    from generaldataagent.evaluation.experience_comparison import compare_experience_runs

    _, run = run_manifest
    actor = dict(user_id="u", tenant_id="t", project_id="p", data_domains=["payments"])
    baseline, _ = run(["What is the fee?"])
    candidate, _ = run(["What is the fee?"], experience_actor=actor)
    result = compare_experience_runs(baseline, candidate)
    assert result["n"] == 1
    assert result["improvements"] == result["regressions"] == []
    assert result["efficiency"]["candidate"]["tokens"] == 0
    assert result["production_accepted"] is False


def test_invalid_experience_database_stops_before_worker(run_manifest, tmp_path, monkeypatch):
    runner, run = run_manifest
    source = tmp_path / "invalid.sqlite"
    source.write_bytes(b"not a SQLite database")
    invoked = []
    monkeypatch.setattr(runner, "run_isolated", lambda *args, **kwargs: invoked.append(True))
    actor = dict(user_id="u", tenant_id="t", project_id="p", data_domains=["payments"])
    with pytest.raises(sqlite3.DatabaseError):
        run(["What is the fee?"], experience_actor=actor, experience_database=source)
    assert invoked == []
    assert not (tmp_path / "run-1" / "report.json").exists()
