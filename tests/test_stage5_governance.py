from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.business.governance import (
    Capability,
    CapabilityRegistry,
    Feedback,
    FeedbackRegistry,
)
from generaldataagent.contracts import ActorContext

HASH = "sha256:" + "a" * 64
ACTOR = ActorContext(user_id="alice", tenant_id="t", project_id="p", data_domains=("sales",))


def capability(**updates):
    values = dict(
        name="safe-model",
        version="v1",
        kind="model",
        content_hash=HASH,
        owner="operator",
        tenant_id="t",
        project_id="p",
        data_domains=("sales",),
        regression_evidence_hash=HASH,
        security_evidence_hash=HASH,
        rollback_version="disabled",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    return Capability(**(values | updates))


def admitted_registry(tmp_path, spec=None):
    spec = spec or capability()
    registry = CapabilityRegistry(tmp_path / "admission.db", clock=lambda: datetime.now(UTC))
    registry.propose(spec)
    registry.review(spec.name, spec.version, reviewer_id="quality-reviewer", discipline="quality")
    registry.review(spec.name, spec.version, reviewer_id="security-reviewer", discipline="security")
    registry.activate(spec.name, spec.version)
    return registry


@pytest.mark.parametrize("kind", ["agent", "skill", "connector", "model"])
def test_capability_pinned_independent_review_lifecycle(tmp_path, kind):
    spec = capability(kind=kind)
    registry = CapabilityRegistry(tmp_path / "registry", clock=lambda: datetime.now(UTC))
    registry.propose(spec)
    with pytest.raises(PermissionError):
        registry.require(spec.name, spec.version, HASH, ACTOR)
    with pytest.raises(PermissionError):
        registry.activate(spec.name, spec.version)
    with pytest.raises(PermissionError):
        registry.review(spec.name, spec.version, reviewer_id=spec.owner, discipline="quality")
    registry.review(spec.name, spec.version, reviewer_id="q", discipline="quality")
    with pytest.raises(ValueError):
        registry.review(spec.name, spec.version, reviewer_id="q", discipline="security")
    registry.review(spec.name, spec.version, reviewer_id="s", discipline="security")
    registry.activate(spec.name, spec.version)
    assert registry.require(spec.name, spec.version, HASH, ACTOR) == spec
    with pytest.raises(PermissionError):
        registry.require(spec.name, spec.version, "sha256:" + "b" * 64, ACTOR)
    with pytest.raises(PermissionError):
        registry.require(
            spec.name, spec.version, HASH, ACTOR.model_copy(update={"project_id": "other"})
        )
    registry.revoke(spec.name, spec.version)
    with pytest.raises(PermissionError):
        registry.require(spec.name, spec.version, HASH, ACTOR)
    with pytest.raises(PermissionError):
        registry.activate(spec.name, spec.version)
    with pytest.raises(ValueError):
        registry.propose(spec)


def test_feedback_requires_task_scope_review_and_split_isolation(tmp_path):
    registry = FeedbackRegistry(tmp_path / "feedback")
    feedback = Feedback(
        task_id=uuid4(),
        result_hash=HASH,
        category="correctness",
        rating=2,
        redacted_question="Revenue by region",
        expected_outcome="Include all regions",
    )
    with pytest.raises(PermissionError):
        registry.submit(feedback, ACTOR)
    registry.register_task(feedback.task_id, ACTOR, HASH)
    registry.register_task(feedback.task_id, ACTOR, HASH)
    with pytest.raises(ValueError):
        registry.register_task(feedback.task_id, ACTOR, "sha256:" + "b" * 64)
    registry.submit(feedback, ACTOR)
    with pytest.raises(ValueError):
        registry.submit(feedback, ACTOR)
    with pytest.raises(PermissionError):
        registry.review(feedback.feedback_id, reviewer_id="alice", accepted=True)
    with pytest.raises(PermissionError):
        registry.freeze(uuid4(), (feedback.feedback_id,), split="evaluation")
    registry.review(feedback.feedback_id, reviewer_id="q", accepted=True)
    registry.review(feedback.feedback_id, reviewer_id="s", accepted=True)
    with pytest.raises(ValueError):
        registry.review(feedback.feedback_id, reviewer_id="s", accepted=True)
    dataset_id = uuid4()
    frozen = registry.freeze(dataset_id, (feedback.feedback_id,), split="evaluation")
    assert frozen["examples"][0]["rating"] == 2
    with pytest.raises(ValueError, match="immutable"):
        registry.freeze(dataset_id, (feedback.feedback_id,), split="evaluation")
    with pytest.raises(PermissionError, match="overlap"):
        registry.freeze(uuid4(), (feedback.feedback_id,), split="training")
    # Rewording a task cannot move the same evidence into training.
    other = feedback.model_copy(update={"task_id": uuid4(), "feedback_id": uuid4()})
    registry.register_task(other.task_id, ACTOR, HASH)
    registry.submit(other, ACTOR)
    registry.review(other.feedback_id, reviewer_id="q", accepted=True)
    registry.review(other.feedback_id, reviewer_id="s", accepted=True)
    with pytest.raises(PermissionError, match="overlap"):
        registry.freeze(uuid4(), (other.feedback_id,), split="training")


def test_feedback_dlp_tampering_and_negative_review(tmp_path):
    registry = FeedbackRegistry(tmp_path / "feedback")
    item = Feedback(
        task_id=uuid4(),
        result_hash=HASH,
        category="safety",
        rating=1,
        redacted_question="Review report",
        expected_outcome="Reject secrets",
    )
    registry.register_task(item.task_id, ACTOR, HASH)
    with pytest.raises(PermissionError, match="dlp"):
        registry.submit(
            item.model_copy(update={"expected_outcome": "password=SecretValueLong"}), ACTOR
        )
    registry.submit(item, ACTOR)
    registry.review(item.feedback_id, reviewer_id="q", accepted=True)
    registry.review(item.feedback_id, reviewer_id="s", accepted=False)
    with pytest.raises(PermissionError):
        registry.freeze(uuid4(), (item.feedback_id,), split="evaluation")
    import sqlite3

    with sqlite3.connect(registry.path) as db:
        db.execute("UPDATE feedback SET hash='changed'")
    with pytest.raises(PermissionError, match="integrity"):
        registry.review(item.feedback_id, reviewer_id="third", accepted=True)


def test_frozen_dataset_read_integrity_and_authorization(tmp_path):
    registry = FeedbackRegistry(tmp_path / "feedback")
    feedback = Feedback(
        task_id=uuid4(),
        result_hash=HASH,
        category="usability",
        rating=3,
        redacted_question="Revenue",
        expected_outcome="Cite the source",
    )
    registry.register_task(feedback.task_id, ACTOR, HASH)
    registry.submit(feedback, ACTOR)
    for reviewer in ("q", "s"):
        registry.review(feedback.feedback_id, reviewer_id=reviewer, accepted=True)
    dataset_id = uuid4()
    frozen = registry.freeze(dataset_id, (feedback.feedback_id,), split="evaluation")
    assert registry.read_dataset(dataset_id, ACTOR) == frozen
    with pytest.raises(PermissionError):
        registry.read_dataset(dataset_id, ACTOR.model_copy(update={"user_id": "other"}))
    import sqlite3

    with sqlite3.connect(registry.path) as db:
        db.execute("UPDATE feedback_datasets SET hash='changed'")
    with pytest.raises(PermissionError, match="integrity"):
        registry.read_dataset(dataset_id, ACTOR)
