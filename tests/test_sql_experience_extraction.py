"""Scripted real SQLite recoveries produce bounded, unreviewed generic lessons."""

import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from uuid import UUID

import pytest

from generaldataagent.business.actions import content_hash
from generaldataagent.business.experience import ExperienceError, ExperienceStore
from generaldataagent.business.experience_batch import capture_batch
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.experience.extraction import extract_experiences, extract_rule_experience
from generaldataagent.experience.sql_extraction import (
    SQL_REPAIR_GUIDANCE,
    extract_sql_experiences,
    observe_sql_repair,
)

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("analytics",))
NOW = datetime(2026, 10, 30, tzinfo=UTC)
BAD_SQL = {
    "unknown_column": "SELECT missing FROM observations",
    "ambiguous_column": ("SELECT amount FROM observations a JOIN observations b ON a.id = b.id"),
    "sql_syntax": "SELECT amount FROM observations WHERE (",
    "unsupported_function": "SELECT STDDEV_POP(amount) FROM observations",
    "function_arguments": "SELECT ROUND(amount, 1, 2) FROM observations",
}


def scripted_trace(
    root,
    queries,
    *,
    task_id="synthetic",
    table="observations",
    profile="sql-repair",
    experience=None,
    observed_messages=None,
):
    database = root / "source.sqlite"
    with sqlite3.connect(database) as db:
        db.execute(f'CREATE TABLE "{table}" (id INTEGER, amount REAL)')
        db.executemany(f'INSERT INTO "{table}" VALUES(?, ?)', [(1, 2.5), (2, 4.0)])

    class Model:
        def __init__(self):
            self.calls = iter(queries)

        def invoke(self, messages):
            from types import SimpleNamespace

            query = next(self.calls)
            if observed_messages is not None:
                observed_messages.append(list(messages))
            return SimpleNamespace(
                content=json.dumps(query if isinstance(query, dict) else {"sql": query}),
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    result = BoundedSQLAgent(
        Model(), tool_profile=profile, max_steps=len(queries), max_repairs=6, experience=experience
    ).run(
        Task(
            "bird-mini-dev-sqlite" if profile == "sql-precision" else "synthetic",
            task_id,
            "Observe the measure",
            "answer",
            "easy",
            "source",
        ),
        database,
    )
    return result["trace"]


@pytest.fixture
def recovered(tmp_path):
    return scripted_trace(tmp_path, [BAD_SQL["unknown_column"], "SELECT amount FROM observations"])


def extract(trace):
    return extract_experiences(trace, documents="", owner=ACTOR.user_id, created_at=NOW)


@pytest.mark.parametrize("failure_class", list(BAD_SQL))
def test_real_sql_errors_capture_candidates_with_exact_sources_and_review_gate(
    tmp_path, failure_class
):
    trace = scripted_trace(tmp_path, [BAD_SQL[failure_class], "SELECT amount FROM observations"])
    assert [event["status"] for event in trace] == ["error", "ok"]
    assert trace[0]["code"] == failure_class
    assert trace[0]["tool"] == "sql"
    (candidate,) = extract(trace)
    assert candidate.template_version == "sql-repair-v1"
    assert candidate.record.failure_class == failure_class
    assert candidate.record.tool_scope == ("sql",)
    assert (candidate.failure_step, candidate.recovery_step) == (1, 2)
    assert extract_rule_experience(trace, documents="", owner="u", created_at=NOW) == ()
    trace_bytes = json.dumps(trace, indent=2).encode()
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    store.capture_from_extraction(
        candidate, ACTOR, trace_bytes=trace_bytes, document_bytes=b"", extractor_hash="a" * 64
    )
    source = store.extraction_source(candidate.record.experience_id, ACTOR)
    assert source["trace_hash"] == hashlib.sha256(trace_bytes).hexdigest()
    assert source["result_hash"] == trace[1]["result_hash"]
    assert store.state(candidate.record.experience_id, ACTOR) == "candidate"
    assert store.audit()["consistent"]
    assert store.retrieve(failure_class=failure_class, tool="sql", actor=ACTOR) == ()
    store.review(
        candidate.record.experience_id,
        reviewer_id="independent-test-user",
        accepted=True,
        actor=ACTOR,
    )
    assert store.retrieve(failure_class=failure_class, tool="sql", actor=ACTOR) == (
        candidate.record,
    )


@pytest.mark.parametrize(
    "change",
    [
        "old_trace",
        "context",
        "query",
        "result",
        "receipt",
        "receipt_types",
        "wrong_tool",
        "no_success",
        "interleaved",
        "step",
        "unknown_code",
        "bad_code_type",
        "unsafe",
        "same_query",
        "too_long",
        "row_count",
    ],
)
def test_unbound_changed_or_unsupported_observations_produce_no_candidate(recovered, change):
    trace = copy.deepcopy(recovered)
    failure, success = trace
    if change == "old_trace":
        failure.pop("sql_repair_context_hash")
    elif change == "context":
        success["sql_repair_context_hash"] = "b" * 64
    elif change == "query":
        success["sql"] = "SELECT id FROM observations"
    elif change == "result":
        success["result_hash"] = "c" * 64
    elif change == "receipt":
        success.pop("sql_repair_evidence")
    elif change == "receipt_types":
        success["sql_repair_evidence"]["failure_step"] = True
    elif change == "wrong_tool":
        failure["tool"] = "rules"
    elif change == "no_success":
        success["status"] = "error"
    elif change == "interleaved":
        trace.insert(1, {"status": "planned", "step": 1})
    elif change == "step":
        success["step"] = True
    elif change == "unknown_code":
        failure["code"] = "policy_rejection"
    elif change == "bad_code_type":
        failure["code"] = {}
    elif change == "unsafe":
        success["sql"] = "DELETE FROM observations"
    elif change == "same_query":
        success["sql"] = failure["attempted_sql"] + " "
    elif change == "too_long":
        failure["attempted_sql"] = "x" * 10001
    else:
        success["row_count"] = True
    assert extract(trace) == ()


def test_failed_mutation_is_not_reclassified_as_a_syntax_repair(recovered):
    recovered[0].update(code="sql_syntax", error="SqlGuardError", attempted_sql="DROP TABLE x")
    assert observe_sql_repair(*recovered) is None


@pytest.mark.parametrize("profile", ["sql", "sql-analysis", "sql-repair", "sql-precision"])
def test_each_supported_agent_profile_emits_bound_observations(tmp_path, profile):
    trace = scripted_trace(
        tmp_path, [BAD_SQL["unknown_column"], "SELECT amount FROM observations"], profile=profile
    )
    assert len(extract(trace)) == 1


def test_different_real_task_contexts_cannot_be_spliced_into_a_repair(tmp_path):
    traces = []
    for task_id in ("first", "second"):
        folder = tmp_path / task_id
        folder.mkdir()
        traces.append(
            scripted_trace(
                folder,
                [BAD_SQL["unknown_column"], "SELECT amount FROM observations"],
                task_id=task_id,
            )
        )
    assert extract([traces[0][0], traces[1][1]]) == ()


def test_observation_to_final_submission_is_not_a_same_purpose_repair(tmp_path):
    trace = scripted_trace(
        tmp_path, [BAD_SQL["unknown_column"], {"final_sql": "SELECT amount FROM observations"}]
    )
    assert [event["status"] for event in trace] == ["error", "ok"]
    assert "sql_repair_evidence" not in trace[1]
    assert extract(trace) == ()


def test_single_trace_cli_captures_the_new_family(tmp_path, recovered, monkeypatch):
    import sys

    from test_stage10_experience_extraction import cli

    module = cli(tmp_path, monkeypatch)
    trace_path, document_path = tmp_path / "trace.json", tmp_path / "documents.txt"
    trace_path.write_text(json.dumps(recovered))
    document_path.write_text("")
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "extract",
            "--trace",
            str(trace_path),
            "--documents",
            str(document_path),
            "--actor",
            ACTOR.model_dump_json(),
            "--output",
            str(output),
        ],
    )
    module.main()
    report = json.loads((output / "report.json").read_text())
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["template_version"] == "sql-repair-v1"
    assert report["independent_review"] == "not_run"


def test_reviewed_sql_lesson_reaches_the_next_call_on_another_synthetic_asset(tmp_path, recovered):
    from generaldataagent.business.experience import experience_hints

    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "store.sqlite", clock=lambda: NOW)
    store.capture_from_extraction(
        candidate,
        ACTOR,
        trace_bytes=json.dumps(recovered).encode(),
        document_bytes=b"",
        extractor_hash="a" * 64,
    )
    hints = experience_hints(store, ACTOR)
    assert hints("unknown_column", "sql") == ()
    store.review(
        candidate.record.experience_id,
        reviewer_id="independent-test-user",
        accepted=True,
        actor=ACTOR,
    )
    target = tmp_path / "second-asset"
    target.mkdir()
    observed = []
    trace = scripted_trace(
        target,
        ["SELECT missing FROM other_asset", "SELECT amount FROM other_asset"],
        table="other_asset",
        experience=hints,
        observed_messages=observed,
    )
    feedback = [json.loads(text) for role, text in observed[1] if role == "human"]
    offered = [item["reviewed_experience"] for item in feedback if "reviewed_experience" in item]
    assert len(offered) == 1
    assert offered[0][0]["repair"] == candidate.record.repair
    assert trace[1]["status"] == "ok"


def test_lessons_do_not_copy_query_names_literals_or_arbitrary_trace_text(tmp_path):
    table = "private_sentinel"
    trace = scripted_trace(
        tmp_path, [f"SELECT missing FROM {table}", f"SELECT amount FROM {table}"], table=table
    )
    trace[0]["question"] = "expected_answer sentinel"
    trace[1]["unrelated"] = "reference sql sentinel"
    (candidate,) = extract(trace)
    assert "sentinel" not in candidate.record.model_dump_json()
    assert "private_" not in candidate.record.text()
    assert "Tool acceptance alone" in candidate.record.counterexample


def test_multiple_classes_are_ordered_and_repeated_classes_are_bounded(tmp_path):
    trace = scripted_trace(
        tmp_path,
        [
            BAD_SQL["unknown_column"],
            "SELECT amount FROM observations",
            BAD_SQL["sql_syntax"],
            "SELECT id FROM observations",
            BAD_SQL["unknown_column"],
            "SELECT amount + 0 FROM observations",
        ],
    )
    candidates = extract(trace)
    assert [(item.record.failure_class, item.failure_step) for item in candidates] == [
        ("unknown_column", 1),
        ("sql_syntax", 3),
    ]
    assert len(extract_sql_experiences(trace * 10, owner="u", created_at=NOW)) == 2
    assert set(BAD_SQL) == set(SQL_REPAIR_GUIDANCE)


def test_store_rederives_sql_candidates_instead_of_trusting_candidate_hashes(tmp_path, recovered):
    (candidate,) = extract(recovered)
    changed = candidate.model_copy(
        update={
            "record": candidate.record.model_copy(update={"repair": "substitute another lesson"})
        }
    )
    store = ExperienceStore(tmp_path / "store.sqlite", clock=lambda: NOW)
    with pytest.raises(ExperienceError, match="experience_extraction_source_denied"):
        store.capture_from_extraction(
            changed,
            ACTOR,
            trace_bytes=json.dumps(recovered).encode(),
            document_bytes=b"",
            extractor_hash="a" * 64,
        )
    assert store.audit()["records"] == 0


def test_batch_and_reopened_receipts_keep_new_candidates_unreviewed(tmp_path, recovered):
    (tmp_path / "trace.json").write_text(json.dumps(recovered))
    (tmp_path / "empty.json").write_text("[]")
    (tmp_path / "documents.txt").write_text("")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"id": name, "trace": trace, "documents": "documents.txt"}
                for name, trace in (
                    ("sql", "trace.json"),
                    ("duplicate", "trace.json"),
                    ("empty", "empty.json"),
                )
            ]
        )
    )
    report = capture_batch(
        manifest, tmp_path / "out", actor=ACTOR, created_at=NOW, extractor_hash="a" * 64
    )
    assert report["counts"] == {"captured": 1, "duplicate": 1, "no_candidate": 1}
    assert report["planned_items"] == 3 and report["candidate_count"] == 1
    assert report["independent_review"] == "not_run"
    store = ExperienceStore(tmp_path / "out/candidates.sqlite", clock=lambda: NOW, read_only=True)
    identifier = UUID(report["items"][0]["candidate_ids"][0])
    assert store.extraction_source(identifier, ACTOR)["template_version"] == "sql-repair-v1"
    assert store.retrieve(failure_class="unknown_column", tool="sql", actor=ACTOR) == ()


def test_receipt_template_cannot_be_swapped_to_a_different_known_family(tmp_path, recovered):
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "store.sqlite", clock=lambda: NOW)
    store.capture_from_extraction(
        candidate,
        ACTOR,
        trace_bytes=json.dumps(recovered).encode(),
        document_bytes=b"",
        extractor_hash="a" * 64,
    )
    with sqlite3.connect(store.path) as db:
        (body,) = db.execute("SELECT body FROM experience_extraction_sources").fetchone()
        source = json.loads(body) | {"template_version": "rule-excerpt-v1"}
        db.execute(
            "UPDATE experience_extraction_sources SET body=?,hash=?",
            (json.dumps(source), content_hash(source)),
        )
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.extraction_source(candidate.record.experience_id, ACTOR)
