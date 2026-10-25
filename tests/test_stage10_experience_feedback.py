"""Only scoped frozen training feedback may seed a feedback-bound candidate."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from generaldataagent.business.experience import ExperienceError, ExperienceRecord, ExperienceStore
from generaldataagent.business.governance import Feedback, FeedbackRegistry
from generaldataagent.contracts import ActorContext

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
NOW = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture
def sources(tmp_path):
    registry = FeedbackRegistry(tmp_path / "feedback.sqlite")
    item = Feedback(
        task_id=uuid4(),
        result_hash="sha256:" + "a" * 64,
        category="grounding",
        rating=2,
        redacted_question="private question sentinel",
        expected_outcome="private outcome sentinel",
    )
    registry.register_task(item.task_id, ACTOR, item.result_hash)
    registry.submit(item, ACTOR)
    for reviewer in ("quality", "security"):
        registry.review(item.feedback_id, reviewer_id=reviewer, accepted=True)
    store = ExperienceStore(tmp_path / "experience.sqlite", clock=lambda: NOW)
    record = ExperienceRecord(
        failure_class="rule_semantics_excerpt_missing",
        applies_when=("a required excerpt is missing from supplied documents",),
        does_not_apply_when=("the documentation does not establish the semantics",),
        repair="locate a verbatim source passage before retrying",
        counterexample="inventing documentation remains invalid",
        tool_scope=("rules",),
        owner=ACTOR.user_id,
        created_at=NOW,
    )
    return registry, item, store, record


def capture(sources, dataset_id, *, feedback_id=None, actor=ACTOR):
    registry, item, store, record = sources
    return store.capture_from_feedback(
        record,
        actor,
        registry=registry,
        dataset_id=dataset_id,
        feedback_id=feedback_id or item.feedback_id,
    )


def test_training_feedback_produces_a_candidate_with_no_label_copy(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    dataset = registry.freeze(dataset_id, (item.feedback_id,), split="training")
    assert capture(sources, dataset_id) == str(record.experience_id)
    assert store.state(record.experience_id, ACTOR) == "candidate"
    assert store.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR) == ()
    with sqlite3.connect(store.path) as db:
        body, digest = db.execute("SELECT body,hash FROM experience_feedback_sources").fetchone()
        text = db.execute("SELECT body FROM experiences").fetchone()[0]
    from generaldataagent.business.actions import content_hash

    source = json.loads(body)
    assert digest == content_hash(source)
    assert source == {
        "experience_id": str(record.experience_id),
        "dataset_id": str(dataset_id),
        "dataset_hash": dataset["content_hash"],
        "feedback_id": str(item.feedback_id),
        "task_id": str(item.task_id),
        "result_hash": item.result_hash,
        "split": "training",
    }
    assert "sentinel" not in body + text
    store.review(
        record.experience_id,
        reviewer_id="experience-reviewer",
        accepted=True,
        actor=ACTOR,
        registry=registry,
    )
    assert store.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR) == (
        record,
    )
    # The registry continues to enforce the same task/result split exclusion.
    with pytest.raises(PermissionError, match="evaluation_training_overlap"):
        registry.freeze(uuid4(), (item.feedback_id,), split="evaluation")


@pytest.mark.parametrize(
    "denial", ["evaluation", "foreign_scope", "nonmember", "missing", "tampered"]
)
def test_denied_feedback_source_leaves_no_candidate_or_provenance(sources, denial):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(
        dataset_id,
        (item.feedback_id,),
        split="evaluation" if denial == "evaluation" else "training",
    )
    actor = ACTOR.model_copy(update={"tenant_id": "other"}) if denial == "foreign_scope" else ACTOR
    if denial == "tampered":
        with sqlite3.connect(registry.path) as db:
            db.execute("UPDATE feedback_datasets SET hash='invalid'")
    error = ExperienceError if denial in {"evaluation", "nonmember"} else PermissionError
    with pytest.raises(error):
        capture(
            sources,
            uuid4() if denial == "missing" else dataset_id,
            feedback_id=uuid4() if denial == "nonmember" else None,
            actor=actor,
        )
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM experience_feedback_sources").fetchone()[0] == 0


def test_feedback_source_is_scoped_and_available_before_review(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    receipt = store.feedback_source(record.experience_id, ACTOR)
    assert receipt["dataset_id"] == str(dataset_id)
    assert receipt["feedback_id"] == str(item.feedback_id)
    assert receipt["split"] == "training"
    assert "sentinel" not in json.dumps(receipt)
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    assert reader.feedback_source(record.experience_id, ACTOR) == receipt
    receipt["split"] = "evaluation"
    assert reader.feedback_source(record.experience_id, ACTOR)["split"] == "training"
    with pytest.raises(ExperienceError, match="experience_scope_mismatch"):
        reader.feedback_source(record.experience_id, ACTOR.model_copy(update={"user_id": "other"}))


@pytest.mark.parametrize(
    "corruption", ["json", "hash", "experience_id", "dataset_hash", "split", "extra", "missing"]
)
def test_corrupt_feedback_source_is_refused(sources, corruption):
    from generaldataagent.business.actions import content_hash

    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        body, digest = db.execute("SELECT body,hash FROM experience_feedback_sources").fetchone()
        source = json.loads(body)
        if corruption == "json":
            body = "{"
        elif corruption == "hash":
            digest = "invalid"
        else:
            if corruption == "experience_id":
                source["experience_id"] = str(uuid4())
            elif corruption == "dataset_hash":
                source["dataset_hash"] = "invalid"
            elif corruption == "split":
                source["split"] = "evaluation"
            elif corruption == "extra":
                source["expected_outcome"] = "private sentinel"
            else:
                source.pop("task_id")
            body, digest = json.dumps(source), content_hash(source)
        db.execute("UPDATE experience_feedback_sources SET body=?,hash=?", (body, digest))
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.feedback_source(record.experience_id, ACTOR)


@pytest.mark.parametrize("legacy", [False, True])
def test_manual_candidate_has_no_feedback_source_without_mutating_snapshot(sources, legacy):
    registry, item, store, record = sources
    store.capture(record, ACTOR)
    if legacy:
        with sqlite3.connect(store.path) as db:
            db.execute("DROP TABLE experience_feedback_sources")
    before = store.path.read_bytes()
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    assert reader.feedback_source(record.experience_id, ACTOR) is None
    assert store.path.read_bytes() == before


def test_feedback_source_can_be_rechecked_against_the_frozen_registry(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    before = store.path.read_bytes()
    receipt = reader.verify_feedback_source(record.experience_id, ACTOR, registry=registry)
    assert receipt == reader.feedback_source(record.experience_id, ACTOR)
    assert reader.state(record.experience_id, ACTOR) == "candidate"
    assert reader.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR) == ()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["dataset_hash", "feedback_id", "task_id", "result_hash"])
def test_valid_shaped_but_false_source_binding_is_refused(sources, field):
    from generaldataagent.business.actions import content_hash

    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        source = json.loads(
            db.execute("SELECT body FROM experience_feedback_sources").fetchone()[0]
        )
        source[field] = "sha256:" + "b" * 64 if field.endswith("hash") else str(uuid4())
        db.execute(
            "UPDATE experience_feedback_sources SET body=?,hash=?",
            (json.dumps(source), content_hash(source)),
        )
    # Local shape and digest checks pass; registry-backed correspondence must not.
    assert store.feedback_source(record.experience_id, ACTOR) == source
    with pytest.raises(ExperienceError, match="experience_feedback_source_denied"):
        store.verify_feedback_source(record.experience_id, ACTOR, registry=registry)
    assert store.state(record.experience_id, ACTOR) == "candidate"


@pytest.mark.parametrize("denial", ["missing", "evaluation", "foreign_scope", "tampered"])
def test_source_recheck_refuses_unavailable_or_wrong_registry_dataset(sources, denial):
    from generaldataagent.business.actions import content_hash

    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    if denial == "tampered":
        with sqlite3.connect(registry.path) as db:
            db.execute("UPDATE feedback_datasets SET hash='invalid'")
    else:
        other_id = uuid4()
        if denial != "missing":
            other_actor = (
                ACTOR.model_copy(update={"tenant_id": "other"})
                if denial == "foreign_scope"
                else ACTOR
            )
            other = item.model_copy(
                update={
                    "feedback_id": uuid4(),
                    "task_id": uuid4(),
                    "result_hash": "sha256:" + "c" * 64,
                }
            )
            registry.register_task(other.task_id, other_actor, other.result_hash)
            registry.submit(other, other_actor)
            for reviewer in ("quality", "security"):
                registry.review(other.feedback_id, reviewer_id=reviewer, accepted=True)
            registry.freeze(
                other_id,
                (other.feedback_id,),
                split="evaluation" if denial == "evaluation" else "training",
            )
        with sqlite3.connect(store.path) as db:
            source = json.loads(
                db.execute("SELECT body FROM experience_feedback_sources").fetchone()[0]
            )
            source["dataset_id"] = str(other_id)
            db.execute(
                "UPDATE experience_feedback_sources SET body=?,hash=?",
                (json.dumps(source), content_hash(source)),
            )
    error = ExperienceError if denial == "evaluation" else PermissionError
    with pytest.raises(error):
        store.verify_feedback_source(record.experience_id, ACTOR, registry=registry)
    assert store.state(record.experience_id, ACTOR) == "candidate"


def test_source_recheck_requires_a_receipt_and_authorized_experience(sources, monkeypatch):
    registry, item, store, record = sources
    store.capture(record, ACTOR)

    def unexpected_read(*args):
        pytest.fail("registry must not be read without an authorized source receipt")

    monkeypatch.setattr(registry, "read_dataset", unexpected_read)
    with pytest.raises(ExperienceError, match="experience_feedback_source_denied"):
        store.verify_feedback_source(record.experience_id, ACTOR, registry=registry)
    with pytest.raises(ExperienceError, match="experience_scope_mismatch"):
        store.verify_feedback_source(
            record.experience_id, ACTOR.model_copy(update={"user_id": "other"}), registry=registry
        )


def test_feedback_candidate_cannot_be_accepted_without_registry_verification(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with pytest.raises(ExperienceError, match="experience_feedback_source_denied"):
        store.review(record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert store.state(record.experience_id, ACTOR) == "candidate"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experience_reviews").fetchone()[0] == 0


@pytest.mark.parametrize("denial", ["outage", "mismatch", "malformed"])
def test_feedback_acceptance_failure_does_not_write_a_vote(sources, monkeypatch, denial):
    from generaldataagent.business.actions import content_hash

    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    if denial == "outage":

        def unavailable(*args):
            raise PermissionError("synthetic registry unavailable")

        monkeypatch.setattr(registry, "read_dataset", unavailable)
    else:
        with sqlite3.connect(store.path) as db:
            source = json.loads(
                db.execute("SELECT body FROM experience_feedback_sources").fetchone()[0]
            )
            source["result_hash"] = "sha256:" + "b" * 64
            body = "{" if denial == "malformed" else json.dumps(source)
            db.execute(
                "UPDATE experience_feedback_sources SET body=?,hash=?",
                (body, content_hash(source)),
            )
    error = PermissionError if denial == "outage" else ExperienceError
    with pytest.raises(error):
        store.review(
            record.experience_id,
            reviewer_id="reviewer",
            accepted=True,
            actor=ACTOR,
            registry=registry,
        )
    if denial == "malformed":
        # An unreadable receipt now also makes the record unreadable, rather
        # than showing a candidate whose provenance cannot be inspected.
        with pytest.raises(ExperienceError, match="experience_integrity_failure"):
            store.state(record.experience_id, ACTOR)
    else:
        assert store.state(record.experience_id, ACTOR) == "candidate"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experience_reviews").fetchone()[0] == 0


def test_feedback_acceptance_checks_source_under_the_experience_write_lock(sources, monkeypatch):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    read = registry.read_dataset
    observed = []

    def check_lock(*args):
        with (
            sqlite3.connect(store.path, timeout=0) as db,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            db.execute("DELETE FROM experience_feedback_sources")
        observed.append(True)
        return read(*args)

    monkeypatch.setattr(registry, "read_dataset", check_lock)
    store.review(
        record.experience_id,
        reviewer_id="reviewer",
        accepted=True,
        actor=ACTOR,
        registry=registry,
    )
    assert observed == [True]
    assert store.state(record.experience_id, ACTOR) == "active"
    # Withdrawal must remain possible without consulting an unavailable registry.
    store.review(record.experience_id, reviewer_id="rejector", accepted=False, actor=ACTOR)
    assert store.state(record.experience_id, ACTOR) == "candidate"
    assert store.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR) == ()


def test_duplicate_feedback_capture_keeps_one_candidate_and_source(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with pytest.raises(ExperienceError, match="experience_already_exists"):
        capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM experience_feedback_sources").fetchone()[0] == 1


def test_feedback_capture_retains_answer_filter_and_owner_checks(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    for changes, code in (
        ({"repair": "copy EXPECTED_ANSWER"}, "experience_contains_answer_material"),
        ({"owner": "other"}, "experience_owner_mismatch"),
    ):
        with pytest.raises(ExperienceError, match=code):
            store.capture_from_feedback(
                record.model_copy(update=changes),
                ACTOR,
                registry=registry,
                dataset_id=dataset_id,
                feedback_id=item.feedback_id,
            )
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM experience_feedback_sources").fetchone()[0] == 0


def test_provenance_write_failure_rolls_back_candidate(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER deny_source BEFORE INSERT ON experience_feedback_sources "
            "BEGIN SELECT RAISE(ABORT, 'injected source outage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected source outage"):
        capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM experience_feedback_sources").fetchone()[0] == 0


def test_deleting_the_receipt_does_not_restore_the_manual_review_path(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM experience_feedback_sources")
    # Provenance says this record came from feedback, so the missing receipt is
    # corruption rather than a manual record: every path fails closed.
    for call in (
        lambda: store.feedback_source(record.experience_id, ACTOR),
        lambda: store.verify_feedback_source(record.experience_id, ACTOR, registry=registry),
        lambda: store.state(record.experience_id, ACTOR),
        lambda: store.review(
            record.experience_id,
            reviewer_id="reviewer",
            accepted=True,
            actor=ACTOR,
            registry=registry,
        ),
        lambda: store.review(
            record.experience_id, reviewer_id="reviewer", accepted=False, actor=ACTOR
        ),
    ):
        with pytest.raises(ExperienceError, match="experience_integrity_failure"):
            call()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM experience_reviews").fetchone()[0] == 0
    assert store.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR) == ()


def test_capture_records_the_source_kind_atomically(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    manual = record.model_copy(update={"experience_id": uuid4()})
    store.capture(manual, ACTOR)
    with sqlite3.connect(store.path) as db:
        origins = dict(db.execute("SELECT id,origin FROM experience_origins").fetchall())
    assert origins == {
        str(record.experience_id): "feedback",
        str(manual.experience_id): "manual",
    }
    # A failed receipt write must not leave an origin behind either.
    failed = record.model_copy(update={"experience_id": uuid4()})
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER deny_source_again BEFORE INSERT ON experience_feedback_sources "
            "BEGIN SELECT RAISE(ABORT, 'injected source outage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected source outage"):
        store.capture_from_feedback(
            failed,
            ACTOR,
            registry=registry,
            dataset_id=dataset_id,
            feedback_id=item.feedback_id,
        )
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM experience_origins WHERE id=?", (str(failed.experience_id),)
            ).fetchone()[0]
            == 0
        )


def test_manual_records_keep_the_manual_review_path(sources):
    registry, item, store, record = sources
    store.capture(record, ACTOR)
    assert store.feedback_source(record.experience_id, ACTOR) is None
    store.review(record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert store.state(record.experience_id, ACTOR) == "active"


@pytest.mark.parametrize("origin", ["bogus", "manual", "missing"])
def test_contradictory_or_unreadable_origin_marker_is_an_integrity_failure(sources, origin):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        if origin == "missing":
            db.execute("DELETE FROM experience_origins")
        else:
            db.execute("UPDATE experience_origins SET origin=?", (origin,))
    if origin == "missing":
        # The surviving receipt still marks this as feedback-derived: no bypass.
        with pytest.raises(ExperienceError, match="experience_feedback_source_denied"):
            store.review(record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
        assert store.state(record.experience_id, ACTOR) == "candidate"
        return
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.state(record.experience_id, ACTOR)
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.review(record.experience_id, reviewer_id="reviewer", accepted=False, actor=ACTOR)


def test_removing_the_provenance_table_falls_back_to_the_legacy_path(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE experience_feedback_sources")
        db.execute("DROP TABLE experience_origins")
    # Documented residual limit: deleting the whole provenance record set looks
    # exactly like a snapshot taken before provenance existed.
    assert store.feedback_source(record.experience_id, ACTOR) is None
    store.review(record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert store.state(record.experience_id, ACTOR) == "active"


def test_corrupt_feedback_receipt_fails_closed_on_every_read(sources):
    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    store.review(
        record.experience_id,
        reviewer_id="reviewer",
        accepted=True,
        actor=ACTOR,
        registry=registry,
    )
    assert store.state(record.experience_id, ACTOR) == "active"
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE experience_feedback_sources SET body='{'")
    # A record whose captured provenance is unreadable is not usable evidence,
    # even though its own body and review rows are intact.
    for call in (
        lambda: store.state(record.experience_id, ACTOR),
        lambda: store.feedback_source(record.experience_id, ACTOR),
        lambda: store.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR),
    ):
        with pytest.raises(ExperienceError, match="experience_integrity_failure"):
            call()
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        reader.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR)


def test_receipt_body_and_digest_must_agree_on_every_read(sources):
    from generaldataagent.business.actions import content_hash

    registry, item, store, record = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")
    capture(sources, dataset_id)
    store.review(
        record.experience_id,
        reviewer_id="reviewer",
        accepted=True,
        actor=ACTOR,
        registry=registry,
    )
    with sqlite3.connect(store.path) as db:
        seed = json.loads(db.execute("SELECT body FROM experience_feedback_sources").fetchone()[0])
        # A readable, well-typed receipt whose stored digest no longer covers it.
        edited = {**seed, "result_hash": "sha256:" + "b" * 64}
        db.execute(
            "UPDATE experience_feedback_sources SET body=?,hash=?",
            (json.dumps(edited), content_hash(seed)),
        )
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.state(record.experience_id, ACTOR)
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.retrieve(failure_class=record.failure_class, tool="rules", actor=ACTOR)


def test_readonly_port_refuses_feedback_capture_before_source_read(sources, monkeypatch):
    registry, item, store, record = sources
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)

    def unexpected_read(*args):
        pytest.fail("read-only capture must not read feedback")

    monkeypatch.setattr(registry, "read_dataset", unexpected_read)
    with pytest.raises(PermissionError, match="experience_store_read_only"):
        reader.capture_from_feedback(
            record, ACTOR, registry=registry, dataset_id=uuid4(), feedback_id=item.feedback_id
        )
