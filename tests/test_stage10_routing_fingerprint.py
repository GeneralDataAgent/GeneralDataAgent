"""Runner manifests must describe the profiles actually selected for execution."""

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from generaldataagent.business.experience import ExperienceRecord, ExperienceStore
from generaldataagent.contracts import ActorContext
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
    # This test exercises runner orchestration, so it must not need the
    # locally prepared public snapshots (they are not committed). Supply a
    # synthetic data root with only the files the runner reads directly.
    data_root = tmp_path / "stage6"
    (data_root / "dabstep").mkdir(parents=True)
    (data_root / "sources.json").write_text(json.dumps({"sources": [{"path": "synthetic"}]}))
    for name in ("manual.md", "payments-readme.md"):
        (data_root / "dabstep" / name).write_text(f"# {name}\n\nSynthetic fixture.\n")
    monkeypatch.setattr(runner, "DATA_ROOT", data_root)
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
            experience_used=[],
        )

    monkeypatch.setattr(runner, "run_isolated", execute)
    sequence = 0

    def run(
        questions,
        *,
        routing=True,
        profile="sql",
        experience_actor=None,
        experience_database=None,
        receipt_actor=None,
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
        if receipt_actor is not None:
            arguments.extend(["--receipt-actor", json.dumps(receipt_actor)])
        if experience_actor is not None:
            experience_database = experience_database or tmp_path / "experience.sqlite"
            if not experience_database.exists():
                # A real store: the runner now refuses a file without the schema.
                ExperienceStore(experience_database, clock=lambda: None)
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


def test_receipt_opt_in_passes_distinct_explicit_identity_to_every_worker(
    run_manifest, monkeypatch
):
    from uuid import UUID

    runner, run = run_manifest
    execute = runner.run_isolated
    configs = []

    def capture(*args, **kwargs):
        configs.append(kwargs.get("receipt_config"))
        return execute(*args, **kwargs)

    monkeypatch.setattr(runner, "run_isolated", capture)
    actor = ActorContext(
        user_id="operator", tenant_id="research", project_id="receipts", data_domains=("test",)
    )
    report, _ = run(["Count payments", "Mean payment"], receipt_actor=actor.model_dump())
    assert all(config["actor"] == actor.model_dump(mode="json") for config in configs)
    assert len({UUID(config["task_id"]) for config in configs}) == 2
    assert len({config["trace_id"] for config in configs}) == 2
    assert all(Path(config["artifact_root"]).name == "artifacts" for config in configs)
    assert (
        report["system_fingerprint"]["execution_receipts"]["adapter_version"]
        == "evaluation-step-v1"
    )
    off, _ = run(["Count payments"])
    assert configs[-1] is None
    assert off["system_fingerprint"]["execution_receipts"] is None


@pytest.mark.parametrize(
    "routing,mode,limit",
    [(False, "fixed", 125), (True, "per_lane", 185)],
)
def test_protocol_declares_the_deadline_that_will_actually_apply(
    run_manifest, routing, mode, limit
):
    """The report must not declare 125 s while routed lanes may run to 185 s."""
    _, run = run_manifest
    report, _ = run(
        ["What is the fee?"], routing=routing, profile="sql" if routing else "sql-rules"
    )
    protocol = report["protocol"]
    assert protocol["hard_case_deadline_seconds"] == limit
    assert protocol["hard_case_deadline_mode"] == mode
    if routing:
        assert protocol["lane_time_budgets"] == {"short": 90, "analysis": 150, "governed": 180}
    else:
        assert "lane_time_budgets" not in protocol


@pytest.mark.parametrize("routing", [False, True])
def test_protocol_time_ceiling_covers_the_budgets_sent_to_workers(
    run_manifest, monkeypatch, routing
):
    runner, run = run_manifest
    execute = runner.run_isolated
    budgets = []

    def capture(*args, **kwargs):
        budgets.append(kwargs["max_seconds"])
        return execute(*args, **kwargs)

    monkeypatch.setattr(runner, "run_isolated", capture)
    report, _ = run(
        ["What is the fee?", "Find the median amount", "How many payments?"],
        routing=routing,
        profile="sql" if routing else "sql-rules",
    )
    protocol = report["protocol"]
    assert sorted(budgets) == ([90, 150, 180] if routing else [120, 120, 120])
    assert protocol["seconds"] == max(budgets)
    assert protocol["hard_case_deadline_seconds"] == protocol["seconds"] + 5


def test_workers_execute_the_routes_frozen_before_asset_preparation(run_manifest, monkeypatch):
    runner, run = run_manifest
    route_task = runner.route_task
    build = runner.build_csv_database
    execute = runner.run_isolated
    prepared = False
    observed = {}

    def changed_policy(question, instructions="", *, override=None):
        if prepared and override is None:
            # The recorded policy has changed before execution. Recomputing
            # here would make the report describe different profiles/budgets.
            lane = "governed" if "How many" in question else "short"
            return route_task(question, instructions, override=lane)
        return route_task(question, instructions, override=override)

    def prepare(*args, **kwargs):
        nonlocal prepared
        result = build(*args, **kwargs)
        prepared = True
        return result

    def capture(task, *args, **kwargs):
        observed[task.case_id] = {
            key: kwargs[key] for key in ("tool_profile", "max_steps", "max_seconds", "max_repairs")
        }
        return execute(task, *args, **kwargs)

    monkeypatch.setattr(runner, "route_task", changed_policy)
    monkeypatch.setattr(runner, "build_csv_database", prepare)
    monkeypatch.setattr(runner, "run_isolated", capture)
    report, _ = run(["What is the fee?", "Find the median amount", "How many payments?"])
    plans = report["system_fingerprint"]["routing"]["per_case"]
    assert {plan["lane"] for plan in plans.values()} == {"governed", "analysis", "short"}
    for row in report["results"]:
        plan = plans[row["case_id"]]
        assert observed[row["case_id"]] == {key: plan[key] for key in observed[row["case_id"]]}
        assert row["route"] == plan
    assert report["summary"]["n"] == 3
    assert report["summary"]["tokens"] == 0


@pytest.mark.parametrize("routing", [False, True])
def test_protocol_declares_the_step_and_repair_budgets_that_apply(run_manifest, routing):
    """Routed lanes use 5/7/8 steps and 2/3/3 repairs, never the fixed 6 and 3."""
    _, run = run_manifest
    report, _ = run(
        ["What is the fee?"], routing=routing, profile="sql" if routing else "sql-rules"
    )
    protocol = report["protocol"]
    if routing:
        assert protocol["steps"] == 8
        assert protocol["lane_step_budgets"] == {"short": 5, "analysis": 7, "governed": 8}
        assert protocol["lane_repair_budgets"] == {"short": 2, "analysis": 3, "governed": 3}
        assert protocol["max_repairs_per_case"] == 3
    else:
        assert protocol["steps"] == 6
        assert protocol["max_repairs_per_case"] == 3
        assert "lane_step_budgets" not in protocol
        assert "lane_repair_budgets" not in protocol


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


@pytest.mark.parametrize("routing", [False, True])
@pytest.mark.parametrize("source", ["business/rules/payments.py", "business/rules/application.py"])
def test_rule_runner_fingerprint_includes_shared_rule_services(run_manifest, routing, source):
    import hashlib

    _, run = run_manifest
    report, _ = run(
        ["What is the fee?"], routing=routing, profile="sql" if routing else "sql-rules"
    )
    expected = hashlib.sha256((ROOT / "src/generaldataagent" / source).read_bytes()).hexdigest()
    assert report["system_fingerprint"]["stage9_sources"][source] == expected


@pytest.mark.parametrize("routing", [False, True])
def test_shared_payment_parser_change_blocks_same_system_repeats(
    run_manifest, monkeypatch, routing
):
    from generaldataagent.evaluation.stage_six_stability import repeated_evidence

    _, run = run_manifest
    before, _ = run(
        ["What is the fee?"], routing=routing, profile="sql" if routing else "sql-rules"
    )
    original = Path.read_bytes
    source = ROOT / "src/generaldataagent/business/rules/payments.py"

    def changed(path):
        content = original(path)
        return content + b"\n# changed date parser\n" if path == source else content

    monkeypatch.setattr(Path, "read_bytes", changed)
    after, _ = run(["What is the fee?"], routing=routing, profile="sql" if routing else "sql-rules")
    assert before["protocol_hash"] == after["protocol_hash"]
    assert before["cohort_hash"] == after["cohort_hash"]
    assert before["system_fingerprint"] != after["system_fingerprint"]
    with pytest.raises(ValueError, match="incomparable repeated-run system_fingerprint"):
        repeated_evidence([before, after])


def test_missing_payment_parser_cannot_be_omitted_from_the_stage9_fingerprint(monkeypatch):
    from generaldataagent.evaluation.stage_nine import stage9_fingerprint

    original = Path.read_bytes
    source = ROOT / "src/generaldataagent/business/rules/payments.py"

    def missing(path):
        if path == source:
            raise FileNotFoundError("synthetic missing payment parser")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", missing)
    with pytest.raises(FileNotFoundError, match="synthetic missing payment parser"):
        stage9_fingerprint(ROOT)


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
    if wal:
        with sqlite3.connect(source) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
    actor_context = ActorContext(
        user_id="u", tenant_id="t", project_id="p", data_domains=("payments",)
    )
    store = ExperienceStore(source, clock=lambda: datetime(2026, 10, 24, tzinfo=UTC))
    store.capture(
        ExperienceRecord(
            failure_class="rule_semantics_excerpt_missing",
            applies_when=("reviewed-before-run applies",),
            does_not_apply_when=("the documentation does not establish the semantics",),
            repair="locate a verbatim source passage before retrying",
            counterexample="inventing documentation remains invalid",
            tool_scope=("rules",),
            owner=actor_context.user_id,
            created_at=datetime(2026, 10, 24, tzinfo=UTC),
        ),
        actor_context,
    )

    def execute(*args, **kwargs):
        path = Path(kwargs["experience_database"])
        observed.append(path)
        assert path != source
        assert path.is_relative_to(tmp_path)
        assert kwargs["experience_store_hash"] == runner.digest(path)
        snap = ExperienceStore(path, clock=lambda: None, read_only=True).audit()
        assert snap["records"] == 1 and snap["consistent"] is True
        with sqlite3.connect(source) as writer:
            writer.execute("CREATE TABLE IF NOT EXISTS later_write (value TEXT)")
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
    assert report["experience_preflight"] == {"records": 1, "consistent": True, "findings": 0}


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
    # The scripted worker delivered no hints, so the declared treatment is
    # reported as not delivered rather than silently credited with no effect.
    assert result["experience_delivery"]["candidate_cases_with_hints"] == 0
    assert "experience_treatment_not_delivered" in result["blocked_by"]
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


def test_database_without_experience_schema_stops_before_worker(
    run_manifest, tmp_path, monkeypatch
):
    runner, run = run_manifest
    source = tmp_path / "wrong.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    invoked = []
    monkeypatch.setattr(runner, "run_isolated", lambda *args, **kwargs: invoked.append(True))
    actor = dict(user_id="u", tenant_id="t", project_id="p", data_domains=["payments"])
    with pytest.raises(SystemExit) as refusal:
        run(["What is the fee?"], experience_actor=actor, experience_database=source)
    assert refusal.value.code == 2
    assert invoked == []
    assert not (tmp_path / "run-1" / "report.json").exists()


def test_experience_store_with_findings_blocks_the_run_before_worker(
    run_manifest, tmp_path, monkeypatch
):
    runner, run = run_manifest
    source = tmp_path / "audited.sqlite"
    actor_context = ActorContext(
        user_id="u", tenant_id="t", project_id="p", data_domains=("payments",)
    )
    store = ExperienceStore(source, clock=lambda: datetime(2026, 10, 24, tzinfo=UTC))
    record = ExperienceRecord(
        failure_class="rule_semantics_excerpt_missing",
        applies_when=("a required excerpt is missing from supplied documents",),
        does_not_apply_when=("the documentation does not establish the semantics",),
        repair="locate a verbatim source passage before retrying",
        counterexample="inventing documentation remains invalid",
        tool_scope=("rules",),
        owner=actor_context.user_id,
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
    )
    store.capture(record, actor_context)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE experiences SET body='{'")
    invoked = []
    monkeypatch.setattr(runner, "run_isolated", lambda *args, **kwargs: invoked.append(True))
    actor = dict(user_id="u", tenant_id="t", project_id="p", data_domains=["payments"])
    with pytest.raises(SystemExit) as refusal:
        run(["What is the fee?"], experience_actor=actor, experience_database=source)
    assert refusal.value.code == 2
    assert invoked == []
    assert not (tmp_path / "run-1" / "report.json").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_experience_preflight_audits_the_executed_snapshot(
    run_manifest, tmp_path, monkeypatch, corrupt
):
    runner, run = run_manifest
    source = tmp_path / "changing.sqlite"
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
    now = datetime(2026, 10, 24, tzinfo=UTC)
    store = ExperienceStore(source, clock=lambda: now)
    original_route = runner.resolve_evaluation_model
    original_worker = runner.run_isolated
    invoked = []

    def change_after_source_audit(**kwargs):
        store.capture(
            ExperienceRecord(
                failure_class="rule_semantics_excerpt_missing",
                applies_when=("a required excerpt is missing",),
                does_not_apply_when=("the documentation does not establish the semantics",),
                repair="locate a verbatim source passage before retrying",
                counterexample="inventing documentation remains invalid",
                tool_scope=("rules",),
                owner=actor.user_id,
                created_at=now,
            ),
            actor,
        )
        if corrupt:
            with sqlite3.connect(source) as db:
                db.execute("UPDATE experiences SET body='{'")
        return original_route(**kwargs)

    def execute(*args, **kwargs):
        invoked.append(Path(kwargs["experience_database"]))
        return original_worker(*args, **kwargs)

    monkeypatch.setattr(runner, "resolve_evaluation_model", change_after_source_audit)
    monkeypatch.setattr(runner, "run_isolated", execute)
    if corrupt:
        with pytest.raises(SystemExit) as refusal:
            run(
                ["What is the fee?"],
                experience_actor=actor.model_dump(mode="json"),
                experience_database=source,
            )
        assert refusal.value.code == 2
        assert invoked == []
        # Preserve the rejected snapshot for diagnosis, without claiming a run.
        assert (tmp_path / "run-1/experience-snapshot.sqlite").is_file()
        assert not (tmp_path / "run-1/report.json").exists()
    else:
        report, _ = run(
            ["What is the fee?"],
            experience_actor=actor.model_dump(mode="json"),
            experience_database=source,
        )
        snapshot_audit = ExperienceStore(invoked[0], clock=lambda: None, read_only=True).audit()
        assert snapshot_audit["records"] == 1
        assert report["experience_preflight"] == {
            "records": snapshot_audit["records"],
            "consistent": snapshot_audit["consistent"],
            "findings": len(snapshot_audit["findings"]),
        }
        assert report["protocol"]["experience_store_hash"] == runner.digest(invoked[0])
        assert report["production_accepted"] is False


def test_clean_experience_store_passes_the_preflight(run_manifest, tmp_path, monkeypatch):
    runner, run = run_manifest
    source = tmp_path / "clean.sqlite"
    actor_context = ActorContext(
        user_id="u", tenant_id="t", project_id="p", data_domains=("payments",)
    )
    store = ExperienceStore(source, clock=lambda: datetime(2026, 10, 24, tzinfo=UTC))
    store.capture(
        ExperienceRecord(
            failure_class="rule_semantics_excerpt_missing",
            applies_when=("a required excerpt is missing from supplied documents",),
            does_not_apply_when=("the documentation does not establish the semantics",),
            repair="locate a verbatim source passage before retrying",
            counterexample="inventing documentation remains invalid",
            tool_scope=("rules",),
            owner=actor_context.user_id,
            created_at=datetime(2026, 10, 24, tzinfo=UTC),
        ),
        actor_context,
    )
    actor = dict(user_id="u", tenant_id="t", project_id="p", data_domains=["payments"])
    report, _ = run(["What is the fee?"], experience_actor=actor, experience_database=source)
    assert report["experience_preflight"] == {
        "records": 1,
        "consistent": True,
        "findings": 0,
    }
    assert report["production_accepted"] is False
