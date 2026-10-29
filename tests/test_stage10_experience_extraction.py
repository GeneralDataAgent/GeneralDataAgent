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
from uuid import UUID, uuid4

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


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "unsupported"},
        {"operation": "applicable"},
        {"empty_list_is_wildcard": "false"},
        {"transaction_value": "NaN"},
        {"unexpected_field": "value"},
    ],
)
def test_bound_but_invalid_rule_arguments_cannot_produce_a_candidate(recovered, changes):
    from generaldataagent.evaluation.experience_extraction import rule_repair_context_hash

    trace = copy.deepcopy(recovered)
    arguments = trace[1]["action"]["rules"]
    arguments.update(changes)
    trace[0]["rule_repair_context_hash"] = rule_repair_context_hash(arguments)
    assert extract(trace) == ()


@pytest.mark.parametrize("operation", [None, "average_fee", "unsupported"])
def test_recovery_evidence_operation_must_match_the_validated_request(recovered, operation):
    trace = copy.deepcopy(recovered)
    evidence = trace[1]["rule_evidence"]
    if operation is None:
        evidence.pop("operation")
    else:
        evidence["operation"] = operation
    trace[1]["result_hash"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, default=str).encode()
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


def cli(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "evals/extract_stage10_experience.py"
    spec = importlib.util.spec_from_file_location("extract_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_digest = module.extraction_source_hash(module.ROOT)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "extraction_source_hash", lambda _: source_digest)
    return module


def test_offline_cli_preserves_provenance_and_review_gate(recovered, tmp_path, monkeypatch):
    module = cli(tmp_path, monkeypatch)
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
    module = cli(tmp_path, monkeypatch)
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


def capture_extracted(store, candidate, actor, trace_bytes, document_bytes):
    return store.capture_from_extraction(
        candidate,
        actor,
        trace_bytes=trace_bytes,
        document_bytes=document_bytes,
        extractor_hash="d" * 64,
    )


def test_extracted_candidate_keeps_a_byte_bound_source_receipt(recovered, tmp_path):
    trace_bytes, document_bytes = json.dumps(recovered).encode(), DOCUMENTS.encode()
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    item = candidate.record
    capture_extracted(store, candidate, ACTOR, trace_bytes, document_bytes)
    source = store.extraction_source(item.experience_id, ACTOR)
    assert source["origin"] == "extraction"
    assert source["failure_step"] == candidate.failure_step
    assert source["recovery_step"] == candidate.recovery_step
    assert source["result_hash"] == candidate.result_hash
    assert source["template_version"] == candidate.template_version
    assert source["trace_hash"] == hashlib.sha256(trace_bytes).hexdigest()
    assert source["documents_hash"] == hashlib.sha256(document_bytes).hexdigest()
    # Receipt carries no question, answer, excerpt text or trace prose.
    body = json.dumps(source)
    assert "sentinel" not in body and "null means all values" not in body
    assert store.state(item.experience_id, ACTOR) == "candidate"
    assert store.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == ()
    # Extraction provenance does not replace independent experience review.
    store.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert store.retrieve(failure_class=item.failure_class, tool="rules", actor=ACTOR) == (item,)


@pytest.mark.parametrize("missing", ["row", "table"])
def test_reopen_never_backfills_missing_extraction_origins(recovered, tmp_path, missing):
    from generaldataagent.business.experience import ExperienceError
    from generaldataagent.experience.schema import ExperienceSchemaError

    (candidate,) = extract(recovered)
    path = tmp_path / "candidates.sqlite"
    store = ExperienceStore(path, clock=lambda: NOW)
    capture_extracted(store, candidate, ACTOR, json.dumps(recovered).encode(), DOCUMENTS.encode())
    item = candidate.record
    store.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    with sqlite3.connect(path) as db:
        if missing == "row":
            db.execute("DELETE FROM experience_origins")
        else:
            db.execute("DROP TABLE experience_origins")
    before = path.read_bytes()
    for read_only in (True, False):
        if missing == "table":
            with pytest.raises(ExperienceSchemaError, match="experience_schema_invalid"):
                ExperienceStore(path, clock=lambda: NOW, read_only=read_only)
        else:
            reopened = ExperienceStore(path, clock=lambda: NOW, read_only=read_only)
            with pytest.raises(ExperienceError, match="experience_integrity_failure"):
                reopened.extraction_source(item.experience_id, ACTOR)
            assert reopened.audit()["consistent"] is False
        assert path.read_bytes() == before


def test_manual_capture_cannot_claim_extraction_provenance(recovered, tmp_path):
    from generaldataagent.business.experience import ExperienceError

    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    store.capture(candidate.record, ACTOR)
    with pytest.raises(ExperienceError, match="experience_origin_mismatch"):
        store.extraction_source(candidate.record.experience_id, ACTOR)


@pytest.mark.parametrize(
    "corruption", ["json", "hash", "experience_id", "template", "steps", "extra", "missing"]
)
def test_corrupt_extraction_receipt_is_refused(recovered, tmp_path, corruption):
    from generaldataagent.business.actions import content_hash

    trace_bytes, document_bytes = json.dumps(recovered).encode(), DOCUMENTS.encode()
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    capture_extracted(store, candidate, ACTOR, trace_bytes, document_bytes)
    with sqlite3.connect(store.path) as db:
        body, digest = db.execute("SELECT body,hash FROM experience_extraction_sources").fetchone()
        source = json.loads(body)
        if corruption == "json":
            body = "{"
        elif corruption == "hash":
            digest = "invalid"
        else:
            if corruption == "experience_id":
                source["experience_id"] = str(uuid4())
            elif corruption == "template":
                source["template_version"] = "other"
            elif corruption == "steps":
                source["recovery_step"] = source["failure_step"] + 5
            elif corruption == "extra":
                source["question"] = "sentinel"
            else:
                source.pop("trace_hash")
            body, digest = json.dumps(source), content_hash(source)
        db.execute("UPDATE experience_extraction_sources SET body=?,hash=?", (body, digest))
    from generaldataagent.business.experience import ExperienceError

    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.extraction_source(candidate.record.experience_id, ACTOR)
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.state(candidate.record.experience_id, ACTOR)


def test_deleting_the_extraction_receipt_is_detected(recovered, tmp_path):
    from generaldataagent.business.experience import ExperienceError

    trace_bytes, document_bytes = json.dumps(recovered).encode(), DOCUMENTS.encode()
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    capture_extracted(store, candidate, ACTOR, trace_bytes, document_bytes)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM experience_extraction_sources")
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.state(candidate.record.experience_id, ACTOR)


def test_extraction_capture_requires_matching_evidence(recovered, tmp_path):
    from generaldataagent.business.experience import ExperienceError

    trace_bytes, document_bytes = json.dumps(recovered).encode(), DOCUMENTS.encode()
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    # The store re-runs the template over the supplied bytes, so a caller cannot
    # bind an unrelated record by merely asserting the right digests.
    for bytes_change, extractor in (
        (b"[]", "d" * 64),
        (trace_bytes, "not-a-digest"),
        (json.dumps(recovered[1:]).encode(), "d" * 64),
    ):
        with pytest.raises(ExperienceError, match="experience_extraction_source_denied"):
            store.capture_from_extraction(
                candidate,
                ACTOR,
                trace_bytes=bytes_change,
                document_bytes=document_bytes,
                extractor_hash=extractor,
            )
    with pytest.raises(ExperienceError, match="experience_extraction_source_denied"):
        store.capture_from_extraction(
            candidate,
            ACTOR,
            trace_bytes=trace_bytes,
            document_bytes=b"",
            extractor_hash="d" * 64,
        )
    with pytest.raises(ExperienceError, match="experience_owner_mismatch"):
        store.capture_from_extraction(
            candidate,
            ACTOR.model_copy(update={"user_id": "other"}),
            trace_bytes=trace_bytes,
            document_bytes=document_bytes,
            extractor_hash="d" * 64,
        )
    # A record edited after extraction no longer matches what the bytes produce.
    edited = candidate.model_copy(
        update={"record": candidate.record.model_copy(update={"repair": "different advice"})}
    )
    with pytest.raises(ExperienceError, match="experience_extraction_source_denied"):
        store.capture_from_extraction(
            edited,
            ACTOR,
            trace_bytes=trace_bytes,
            document_bytes=document_bytes,
            extractor_hash="d" * 64,
        )
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM experience_extraction_sources").fetchone()[0] == 0


def test_extraction_receipt_is_scoped_and_read_only_visible(recovered, tmp_path):
    from generaldataagent.business.experience import ExperienceError

    trace_bytes, document_bytes = json.dumps(recovered).encode(), DOCUMENTS.encode()
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "candidates.sqlite", clock=lambda: NOW)
    capture_extracted(store, candidate, ACTOR, trace_bytes, document_bytes)
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    source = reader.extraction_source(candidate.record.experience_id, ACTOR)
    assert source["origin"] == "extraction"
    with pytest.raises(ExperienceError, match="experience_scope_mismatch"):
        reader.extraction_source(
            candidate.record.experience_id, ACTOR.model_copy(update={"tenant_id": "other"})
        )
    assert store.extraction_source(
        candidate.record.experience_id, ACTOR
    ) == reader.extraction_source(candidate.record.experience_id, ACTOR)


def test_offline_cli_binds_candidates_to_the_exact_input_bytes(recovered, tmp_path, monkeypatch):
    module = cli(tmp_path, monkeypatch)
    trace = tmp_path / "trace.json"
    documents = tmp_path / "manual.txt"
    trace_bytes, document_bytes = json.dumps(recovered).encode(), DOCUMENTS.encode()
    trace.write_bytes(trace_bytes)
    documents.write_bytes(document_bytes)
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
    store = ExperienceStore(output / "candidates.sqlite", clock=lambda: NOW, read_only=True)
    report = json.loads((output / "report.json").read_text())
    experience_id = UUID(report["candidates"][0]["record"]["experience_id"])
    source = store.extraction_source(experience_id, ACTOR)
    assert source["trace_hash"] == hashlib.sha256(trace_bytes).hexdigest()
    assert source["documents_hash"] == hashlib.sha256(document_bytes).hexdigest()
    assert source["extractor_hash"] == report["extractor_hash"]
    assert source["result_hash"] == report["candidates"][0]["result_hash"]
