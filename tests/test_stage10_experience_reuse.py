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
        owner="someone-else",
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
        {"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 1},
        # The identical-failure ceiling allows one repeat before the task stops.
        {"step": 2, "failure_code": FAILURE, "tool": "rules", "n": 1},
    ]
    assert result["failure"] == "repair_budget_exhausted"
    # The counterexample and owner never leave the store.
    assert "inventing an excerpt" not in model.prompts[1]
    assert "someone-else" not in model.prompts[1]


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


def test_hint_count_is_capped(tmp_path):
    database = rule_database(tmp_path)
    store = store_with_reviewed_record(tmp_path)
    hints = experience_hints(store, ACTOR)
    model = RecordingModel(FAILING_CALL)
    result = agent(model, experience=hints, database=database, max_experience=0)
    assert result["experience_used"] == []
    assert all('"reviewed_experience":' not in prompt for prompt in model.prompts)


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
            row("a", [{"step": 1, "failure_code": FAILURE, "tool": "rules", "n": 2}]),
            row("b", []),
            row("c", [{"step": 2, "failure_code": "unknown_column", "tool": "sql", "n": 1}]),
        ]
    )
    assert summary["experience_reuse"] == {
        "cases_with_hints": 2,
        "hints_offered": 3,
        "by_failure_code": {"rule_semantics_excerpt_missing": 2, "unknown_column": 1},
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
