"""Synthetic isolated signed snapshots; not real feedback/production acceptance."""

import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_stage5_production_business import qualified as business_fixture

from generaldataagent.api.app import create_app
from generaldataagent.business.governance import Feedback, FeedbackRegistry
from generaldataagent.business.production_feedback import QualifiedFeedbackRegistry
from generaldataagent.contracts import ActorContext


@pytest.fixture
def feedback(tmp_path):
    q = business_fixture.__wrapped__(tmp_path)
    enrollment = {"bob": {"review"}, "carol": {"review"}, "curator": {"curate"}}
    registry = QualifiedFeedbackRegistry(
        q.state / "feedback",
        broker=q.broker,
        audit=q.audit,
        release=q.release,
        reviewer_admitted=lambda who, scope, operation: operation in enrollment.get(who, set()),
        configuration_version="v1",
    )
    actor = ActorContext(user_id="alice", tenant_id="t", project_id="p", data_domains=("sales",))
    task = uuid4()
    digest = "sha256:" + "a" * 64
    item = Feedback(
        task_id=task,
        result_hash=digest,
        category="correctness",
        rating=4,
        redacted_question="How many orders?",
        expected_outcome="Use the certified count",
    )

    def issue(who, tenant="t"):
        return q.broker.issue(
            user_id=who,
            tenant_id=tenant,
            project_id="p",
            data_domains=frozenset({"sales"}),
            permissions=frozenset({"reviews:submit"}),
        )

    bob, carol, curator = issue("bob"), issue("carol"), issue("curator")
    registry.register_task(task, actor, digest)
    return SimpleNamespace(**locals())


def submit(f):
    f.registry.submit_authenticated(f.item, f.actor, f.q.token)


def test_authenticated_feedback_two_current_reviewers_and_immutable_holdout(feedback):
    f = feedback
    submit(f)
    assert f.registry.production_ready()
    for token in (f.bob, f.carol):
        f.registry.review_authenticated(f.item.feedback_id, token, accepted=True)
    dataset_id = uuid4()
    result = f.registry.freeze_authenticated(
        dataset_id, (f.item.feedback_id,), f.curator, split="evaluation"
    )
    assert f.registry.read_dataset(dataset_id, f.actor) == result
    with pytest.raises(PermissionError):
        f.registry.read_dataset(dataset_id, f.actor.model_copy(update={"user_id": "other"}))
    with pytest.raises(PermissionError):
        f.registry.freeze_authenticated(uuid4(), (f.item.feedback_id,), f.curator, split="training")
    assert f.registry.production_ready()
    with pytest.raises(ValueError):
        f.registry.freeze_authenticated(
            dataset_id, (f.item.feedback_id,), f.curator, split="evaluation"
        )


def test_production_http_accepts_only_authenticated_registered_owner_feedback(feedback):
    f = feedback
    client = TestClient(
        create_app(session_broker=f.q.broker, feedback_registry=f.registry, production_mode=True)
    )
    headers = {
        "authorization": f"Bearer {f.q.token}",
        "x-user-id": "alice",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "sales",
    }
    assert (
        client.post(
            "/v1/business/feedback", json=f.item.model_dump(mode="json"), headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/business/feedback", json=f.item.model_dump(mode="json"), headers=headers
        ).status_code
        == 409
    )
    with pytest.raises(ValueError):
        create_app(
            session_broker=f.q.broker,
            feedback_registry=FeedbackRegistry(f.q.state / "local-feedback"),
            production_mode=True,
        )


@pytest.mark.parametrize("failure", ["foreign_scope", "not_enrolled", "self_review", "revoked"])
def test_feedback_review_never_accepts_spoofed_or_revoked_identity(feedback, failure):
    f = feedback
    submit(f)
    token = f.bob
    if failure == "foreign_scope":
        token = f.issue("bob", tenant="other")
    elif failure == "not_enrolled":
        token = f.issue("eve")
    elif failure == "self_review":
        f.enrollment["alice"] = {"review"}
        token = f.q.broker.issue(
            user_id="alice",
            tenant_id="t",
            project_id="p",
            data_domains=frozenset({"sales"}),
            permissions=frozenset({"reviews:submit"}),
        )
    else:
        f.q.broker.revoke(token)
    with pytest.raises(PermissionError):
        f.registry.review_authenticated(f.item.feedback_id, token, accepted=True)
    with sqlite3.connect(f.registry.path) as db:
        assert db.execute("SELECT count(*) FROM feedback_reviews").fetchone()[0] == 0


def test_reviewer_revoked_after_vote_cannot_promote_dataset(feedback):
    f = feedback
    submit(f)
    for token in (f.bob, f.carol):
        f.registry.review_authenticated(f.item.feedback_id, token, accepted=True)
    f.enrollment.pop("carol")
    with pytest.raises(PermissionError):
        f.registry.freeze_authenticated(
            uuid4(), (f.item.feedback_id,), f.curator, split="evaluation"
        )
    assert f.registry.production_ready()


def test_direct_trusted_id_review_ports_are_not_production_authority(feedback):
    f = feedback
    submit(f)
    with pytest.raises(PermissionError):
        f.registry.review(f.item.feedback_id, reviewer_id="bob", accepted=True)
    with pytest.raises(PermissionError):
        f.registry.freeze(uuid4(), (f.item.feedback_id,), split="evaluation")
    with pytest.raises(PermissionError):
        f.registry.submit(f.item, f.actor)


def test_signed_snapshot_detects_feedback_and_vote_tampering(feedback):
    f = feedback
    submit(f)
    with sqlite3.connect(f.registry.path) as db:
        db.execute(
            "INSERT INTO feedback_reviews VALUES(?,?,1)", (str(f.item.feedback_id), "forged")
        )
    assert not f.registry.production_ready()
    with pytest.raises(PermissionError):
        f.registry.review_authenticated(f.item.feedback_id, f.bob, accepted=True)


def test_audit_outage_and_mid_audit_revocation_fail_closed(feedback):
    f = feedback
    submit(f)
    f.q.outage[0] = True
    with pytest.raises(PermissionError):
        f.registry.review_authenticated(f.item.feedback_id, f.bob, accepted=True)
    f.q.outage[0] = False
    f.q.audit_hook[0] = lambda _: f.q.broker.revoke(f.bob)
    with pytest.raises(PermissionError):
        f.registry.review_authenticated(f.item.feedback_id, f.bob, accepted=True)
    with sqlite3.connect(f.registry.path) as db:
        assert db.execute("SELECT count(*) FROM feedback_reviews").fetchone()[0] == 0
    assert f.registry.production_ready()


def test_unacknowledged_state_change_is_quarantined_not_retried(feedback):
    f = feedback
    submit(f)
    start = len(f.q.captured)

    def fail_completion(_):
        if len(f.q.captured) == start + 2:
            raise TimeoutError("isolated acknowledgement loss")

    f.q.audit_hook[0] = fail_completion
    with pytest.raises(PermissionError):
        f.registry.review_authenticated(f.item.feedback_id, f.bob, accepted=True)
    assert not f.registry.production_ready()
    with pytest.raises(PermissionError):
        f.registry.review_authenticated(f.item.feedback_id, f.carol, accepted=True)


def test_feedback_cannot_promote_local_store_or_start_after_release_revocation(feedback):
    f = feedback
    path = f.q.state / "pilot-feedback"
    FeedbackRegistry(path)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot be promoted"):
        QualifiedFeedbackRegistry(
            path,
            broker=f.q.broker,
            audit=f.q.audit,
            release=f.q.release,
            reviewer_admitted=lambda *_: True,
            configuration_version="v1",
        )
    f.q.release_trust.write_text("[]")
    assert not f.registry.production_ready()
    with pytest.raises(PermissionError):
        submit(f)


def test_feedback_mutation_has_cross_process_and_thread_exclusion(feedback):
    import fcntl
    import os

    f = feedback
    f.registry.lock.acquire()
    try:
        with pytest.raises(ValueError, match="service_busy"):
            submit(f)
    finally:
        f.registry.lock.release()
    fd = os.open(str(f.registry.path) + ".lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="service_busy"):
            submit(f)
    finally:
        os.close(fd)
    assert f.registry.production_ready()
