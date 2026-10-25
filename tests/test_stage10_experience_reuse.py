import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from generaldataagent.business.experience import (
    ExperienceRecord,
    ExperienceStore,
    experience_hints,
)
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task, run_isolated, summarize

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
FAILURE = "rule_semantics_excerpt_missing"


class RecordingModel:
    """Return the same failing tool call every turn and keep the prompts."""

    def __init__(self, call):
        self.call = call
        self.prompts = []

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.prompts.append(json.dumps(messages, default=str))
        return SimpleNamespace(
            content="",
            tool_calls=[self.call],
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


def rule_database(tmp_path):
    import sqlite3

    # A neutral directory: pytest names ``tmp_path`` after the test function, and
    # this test's name would otherwise put "reviewed_experience" into the prompt
    # through the asset path.
    path = tmp_path / "data" / "rules.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.execute("INSERT INTO fees VALUES (?)", (json.dumps({"ID": "A"}),))
    return path


def task(database):
    return Task(
        case_id="reuse-1",
        benchmark="dabstep",
        question="Which fee rules apply?",
        instructions="Answer under answer.",
        difficulty="easy",
        asset=str(database),
    )


def store_with_reviewed_record(tmp_path, *, failure_class=FAILURE, tool="rules", reviewed=True):
    store = ExperienceStore(tmp_path / "experience.sqlite", clock=lambda: datetime.now(UTC))
    record = ExperienceRecord(
        failure_class=failure_class,
        applies_when=("the excerpt is absent from the supplied documents",),
        does_not_apply_when=("the operation only filters supplied facets",),
        repair="quote an excerpt that appears verbatim in the documents",
        counterexample="inventing an excerpt keeps failing the same way",
        tool_scope=(tool,),
        owner=ACTOR.user_id,
        created_at=datetime.now(UTC),
    )
    store.capture(record, ACTOR)
    if reviewed:
        store.review(record.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    return store


FAILING_CALL = {
    "name": "analyze_fee_rules",
    "args": {"operation": "select", "semantics_excerpt": "not in documents"},
}


def agent(model, *, experience, database, **changes):
    return BoundedSQLAgent(
        model,
        max_steps=4,
        tool_profile="sql-rules",
        max_repairs=3,
        experience=experience,
        **changes,
    ).run(task(database), database, documents="null means all")


def test_reviewed_experience_reaches_the_model_after_a_repairable_failure(tmp_path):
    database = rule_database(tmp_path)
    hints = experience_hints(store_with_reviewed_record(tmp_path), ACTOR)
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database)
    # The first prompt is the task; the second carries the recovery material.
    # Match the JSON key, not the bare name: pytest's temp directory is named
    # after this test and appears in the asset path.
    assert '"reviewed_experience":' not in model.prompts[0]
    assert "quote an excerpt that appears verbatim" in model.prompts[1]
    assert result["experience_used"] == [
        {"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 1, "dropped": 0},
        # The identical-failure ceiling allows one repeat before the task stops.
        {"step": 2, "failure_code": FAILURE, "tool": "rules", "n": 1, "dropped": 0},
    ]
    assert result["failure"] == "repair_budget_exhausted"
    # The counterexample and owner never leave the store.
    assert "inventing an excerpt" not in model.prompts[1]
    assert '"owner":' not in model.prompts[1]


@pytest.mark.parametrize("stop", ["last_step", "cost", "time"])
def test_queued_hints_do_not_clear_comparison_delivery_blocker(tmp_path, monkeypatch, stop):
    from test_stage10_experience_comparison import reports

    from generaldataagent.evaluation.experience_comparison import compare_experience_runs

    database = rule_database(tmp_path)
    model = RecordingModel(FAILING_CALL)
    clock = [0.0]
    if stop == "time":
        monkeypatch.setattr(
            "generaldataagent.evaluation.stage_six.time.monotonic", lambda: clock[0]
        )

    def hints(failure_class, tool):
        clock[0] = 121.0
        if stop == "cost":
            runtime.max_cost_usd = 0
        return ({"repair": "quote the supplied source"},)

    runtime = BoundedSQLAgent(
        model,
        max_steps=1 if stop == "last_step" else 4,
        max_seconds=120,
        tool_profile="sql-rules",
        experience=hints,
    )
    result = runtime.run(task(database), database, documents="null means all")
    assert len(model.prompts) == 1
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    assert result["experience_used"] == []
    if stop != "last_step":
        assert result["failure"] == stop + "_budget"
    baseline, candidate = reports()
    candidate["results"][0]["experience_used"] = result["experience_used"]
    candidate["results"][0]["trace"] = result["trace"]
    candidate["summary"] = summarize(candidate["results"])
    comparison = compare_experience_runs(baseline, candidate)
    assert "experience_treatment_not_delivered" in comparison["blocked_by"]


def test_unicode_hint_that_exceeds_rendered_budget_never_enters_prompt(tmp_path):
    database = rule_database(tmp_path)
    store = ExperienceStore(tmp_path / "unicode.sqlite", clock=lambda: datetime.now(UTC))
    record = ExperienceRecord(
        failure_class=FAILURE,
        applies_when=("the excerpt is absent",),
        does_not_apply_when=("only filtering facets",),
        repair="核对来源后再重试" * 50,
        counterexample="inventing source passages remains invalid",
        tool_scope=("rules",),
        owner=ACTOR.user_id,
        created_at=datetime.now(UTC),
    )
    store.capture(record, ACTOR)
    store.review(record.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    hints = experience_hints(store, ACTOR)
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database)
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    assert result["experience_used"] == [
        {"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 0, "dropped": 1},
        {"step": 2, "failure_code": FAILURE, "tool": "rules", "n": 0, "dropped": 1},
    ]
    assert hints.last_budget["used"] == 0


def test_unreviewed_candidates_never_reach_the_model(tmp_path):
    database = rule_database(tmp_path)
    hints = experience_hints(store_with_reviewed_record(tmp_path, reviewed=False), ACTOR)
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database)
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    assert result["experience_used"] == []


def test_a_broken_store_drops_the_hints_and_is_traced(tmp_path):
    database = rule_database(tmp_path)

    def broken(failure_class, tool):
        raise RuntimeError("store offline")

    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=broken, database=database)
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    assert result["experience_used"] == []
    assert [row for row in result["trace"] if row.get("status") == "experience_unavailable"]
    # Advisory material must not change the outcome of an otherwise identical run.
    control = agent(RecordingModel(FAILING_CALL), experience=None, database=database)
    assert result["failure"] == control["failure"]
    assert result["repair_budget"] == control["repair_budget"]


def test_replaced_snapshot_degrades_to_traced_outage_without_changing_budgets(tmp_path):
    import hashlib
    import sqlite3

    from generaldataagent.evaluation.stage_six_worker import _experience_port

    database = rule_database(tmp_path)
    store_with_reviewed_record(tmp_path)
    path = tmp_path / "experience.sqlite"
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE later_change (value TEXT)")
    port = _experience_port(
        {
            "experience_database": str(path),
            "experience_actor": ACTOR.model_dump(mode="json"),
            "experience_store_hash": expected,
        }
    )
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=port, database=database)
    control = agent(RecordingModel(FAILING_CALL), experience=None, database=database)
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    assert result["experience_used"] == []
    assert [row for row in result["trace"] if row.get("status") == "experience_unavailable"] == [
        {"step": 1, "status": "experience_unavailable", "error": "ValueError"},
        {"step": 2, "status": "experience_unavailable", "error": "ValueError"},
    ]
    for key in ("failure", "repair_budget", "tokens", "input_tokens", "output_tokens"):
        assert result[key] == control[key]


def test_corrupt_review_evidence_never_reaches_the_model(tmp_path):
    import sqlite3

    database = rule_database(tmp_path)
    store = store_with_reviewed_record(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM experience_reviews")
    reader = ExperienceStore(store.path, clock=lambda: datetime.now(UTC), read_only=True)
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=experience_hints(reader, ACTOR), database=database)
    control = agent(RecordingModel(FAILING_CALL), experience=None, database=database)
    assert result["experience_used"] == []
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    assert [row for row in result["trace"] if row.get("status") == "experience_unavailable"]
    assert result["failure"] == control["failure"]
    assert result["repair_budget"] == control["repair_budget"]
    assert result["tokens"] == control["tokens"]


def test_failed_lookup_does_not_repeat_previous_budget_drops(tmp_path):
    database = rule_database(tmp_path)
    calls = 0

    def hints(failure_class, tool):
        nonlocal calls
        calls += 1
        if calls == 1:
            hints.last_budget = {"truncated": 2}
            return ()
        raise RuntimeError("store offline")

    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database)
    control = agent(RecordingModel(FAILING_CALL), experience=None, database=database)
    assert calls == 2
    assert result["experience_used"] == [
        {"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 0, "dropped": 2}
    ]
    assert [row for row in result["trace"] if row.get("status") == "experience_unavailable"] == [
        {"step": 2, "status": "experience_unavailable", "error": "RuntimeError"}
    ]
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)
    for field in ("failure", "repair_budget", "tokens", "input_tokens", "output_tokens"):
        assert result[field] == control[field]


def test_hint_count_is_capped(tmp_path):
    database = rule_database(tmp_path)
    store = store_with_reviewed_record(tmp_path)
    hints = experience_hints(store, ACTOR)
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database, max_experience=0)
    assert result["experience_used"] == []
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)


def test_a_disabled_experience_cap_never_consults_the_port(tmp_path):
    database = rule_database(tmp_path)
    calls = []

    def hints(failure_class, tool):
        calls.append(failure_class)
        return ({"failure_class": failure_class, "repair": "unused"},)

    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database, max_experience=0)
    assert calls == []
    assert result["experience_used"] == []
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)


def test_hints_beyond_the_agent_cap_are_reported_as_dropped(tmp_path):
    database = rule_database(tmp_path)
    store = store_with_reviewed_record(tmp_path)
    second = ExperienceRecord(
        failure_class=FAILURE,
        applies_when=("a second reviewed condition holds",),
        does_not_apply_when=("the operation only filters supplied facets",),
        repair="check the second published passage before retrying",
        counterexample="guessing a second passage keeps failing",
        tool_scope=("rules",),
        owner=ACTOR.user_id,
        created_at=datetime.now(UTC),
    )
    third = ExperienceRecord(
        failure_class=FAILURE,
        applies_when=("a second reviewed condition holds",),
        does_not_apply_when=("the operation only filters supplied facets",),
        repair="check the third published passage",
        counterexample="guessing a second passage keeps failing",
        tool_scope=("rules",),
        owner=ACTOR.user_id,
        created_at=datetime.now(UTC),
    )
    for record in (second, third):
        store.capture(record, ACTOR)
        store.review(record.experience_id, reviewer_id="reviewer-1", accepted=True, actor=ACTOR)
    hints = experience_hints(store, ACTOR)
    assert len(hints(FAILURE, "rules")) == 3
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database, max_experience=2)
    # Retrieval is newest first, so the oldest of the three is the one the
    # agent's own cap excludes; it never enters the recovery prompt.
    assert "check the third published passage" in model.prompts[1]
    assert "check the second published passage" in model.prompts[1]
    assert "quote an excerpt that appears verbatim" not in model.prompts[1]
    # The port admitted three hints into the prompt budget; this agent used two.
    # The excluded hint is neither delivered nor accounted for today.
    assert result["experience_used"] == [
        {"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 2, "dropped": 1},
        {"step": 2, "failure_code": FAILURE, "tool": "rules", "n": 2, "dropped": 1},
    ]


@pytest.mark.parametrize("changes", [dict(experience="not callable"), dict(max_experience=-1)])
def test_invalid_experience_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        BoundedSQLAgent(object(), **changes)


def test_experience_hints_require_a_store_and_an_actor(tmp_path):
    store = ExperienceStore(tmp_path / "e.sqlite", clock=lambda: datetime.now(UTC))
    with pytest.raises(ValueError):
        experience_hints(object(), ACTOR)
    with pytest.raises(ValueError):
        experience_hints(store, {"user_id": "u"})


def test_summary_counts_hint_usage():
    def row(case_id, used):
        return {
            "case_id": case_id,
            "passed": False,
            "difficulty": "easy",
            "elapsed_seconds": 1.0,
            "tokens": 1,
            "failure": "repair_budget_exhausted",
            "sub_hits": 0,
            "sub_total": 1,
            "experience_used": used,
        }

    summary = summarize(
        [
            row(
                "a",
                [{"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 2, "dropped": 0}],
            ),
            row("b", []),
            row(
                "c",
                [
                    {
                        "step": 2,
                        "failure_code": "unknown_column",
                        "tool": "sql",
                        "n": 1,
                        "dropped": 0,
                    }
                ],
            ),
        ]
    )
    assert summary["experience_reuse"] == {
        "cases_with_hints": 2,
        "hints_offered": 3,
        "hints_dropped": 0,
        "by_failure_code": {"rule_semantics_excerpt_missing": 2, "unknown_column": 1},
    }


def test_summary_does_not_count_a_case_whose_hints_were_all_dropped():
    def row(case_id, used):
        return {
            "case_id": case_id,
            "passed": False,
            "difficulty": "easy",
            "elapsed_seconds": 1.0,
            "tokens": 1,
            "failure": "repair_budget_exhausted",
            "sub_hits": 0,
            "sub_total": 1,
            "experience_used": used,
        }

    # The character budget dropped every admitted lesson, so nothing reached the
    # model. The case must not be reported as one that received hints, and the
    # dropped count must not disappear from the run summary.
    summary = summarize(
        [
            row(
                "a",
                [{"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 0, "dropped": 2}],
            ),
            row("b", []),
        ]
    )
    assert summary["experience_reuse"] == {
        "cases_with_hints": 0,
        "hints_offered": 0,
        "hints_dropped": 2,
        "by_failure_code": {},
    }


def test_isolated_runs_require_both_store_and_actor(tmp_path):
    database = rule_database(tmp_path)
    with pytest.raises(ValueError, match="experience reuse"):
        run_isolated(
            task(database),
            database,
            app_type="magpie",
            provider_id="magpie",
            experience_database=tmp_path / "e.sqlite",
        )


@pytest.mark.parametrize(
    "missing",
    ["experience_database", "experience_actor", "experience_store_hash"],
)
def test_isolated_experience_arms_cannot_omit_snapshot_provenance(tmp_path, missing):
    database = rule_database(tmp_path)
    arguments = dict(
        experience_database=tmp_path / "e.sqlite",
        experience_actor=ACTOR.model_dump(mode="json"),
        experience_store_hash="a" * 64,
    )
    arguments.pop(missing)
    with pytest.raises(ValueError, match="experience reuse requires"):
        run_isolated(task(database), database, app_type="magpie", provider_id="magpie", **arguments)


@pytest.mark.parametrize(
    "present",
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_isolated_rejects_every_partial_experience_provenance(tmp_path, monkeypatch, present):
    import subprocess
    from types import SimpleNamespace

    database = rule_database(tmp_path)
    calls = []

    def execute(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=json.dumps({"elapsed_seconds": 0}))

    monkeypatch.setattr(subprocess, "run", execute)
    values = dict(
        experience_database=tmp_path / "e.sqlite",
        experience_actor=ACTOR.model_dump(mode="json"),
        experience_store_hash="a" * 64,
    )
    arguments = {
        key: value
        for (key, value), included in zip(values.items(), present, strict=True)
        if included
    }
    with pytest.raises(ValueError, match="experience reuse requires"):
        run_isolated(
            task(database), database, app_type="scripted", provider_id="scripted", **arguments
        )
    assert calls == []


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "sha256:" + "a" * 64,
        "a" * 64 + "\n",
        True,
        42,
        [],
        {},
    ],
)
def test_isolated_rejects_malformed_experience_hash_before_worker(tmp_path, monkeypatch, digest):
    import subprocess
    from types import SimpleNamespace

    database = rule_database(tmp_path)
    calls = []

    def execute(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=json.dumps({"elapsed_seconds": 0}))

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(ValueError, match="invalid experience snapshot hash"):
        run_isolated(
            task(database),
            database,
            app_type="scripted",
            provider_id="scripted",
            experience_database=tmp_path / "e.sqlite",
            experience_actor=ACTOR.model_dump(mode="json"),
            experience_store_hash=digest,
        )
    assert calls == []


def test_off_arm_needs_no_snapshot_provenance(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    database = rule_database(tmp_path)
    seen = {}

    def execute(command, **kwargs):
        seen.update(json.loads(kwargs["input"]))
        return SimpleNamespace(stdout=json.dumps({"elapsed_seconds": 0}))

    monkeypatch.setattr(subprocess, "run", execute)
    run_isolated(task(database), database, app_type="magpie", provider_id="magpie")
    assert seen["experience_database"] is None
    assert seen["experience_store_hash"] is None
