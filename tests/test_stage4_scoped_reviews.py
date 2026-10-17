from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.contracts import ActorContext, HumanReview
from generaldataagent.evaluation.reviews import DuplicateReviewError
from generaldataagent.evaluation.scoped_reviews import ScopedHumanReviewStore
from generaldataagent.security.identity import SessionBroker


def actor(**updates):
    return ActorContext(
        user_id="reviewer", tenant_id="t", project_id="p", data_domains=("commerce",)
    ).model_copy(update=updates)


def review(task_id, reviewer="reviewer"):
    return HumanReview(
        task_id=task_id,
        reviewer_id=reviewer,
        semantic_score=5,
        conclusion_score=5,
        chart_score=5,
        limitations_score=5,
        evidence_uris=("artifact://evidence",),
    )


def test_scope_is_immutable_and_durable(tmp_path):
    path = tmp_path / "reviews.db"
    store = ScopedHumanReviewStore(path)
    task = uuid4()
    store.register_task(task, actor())
    store.register_task(task, actor())
    store.submit_scoped(review(task), actor())
    store = ScopedHumanReviewStore(path)
    assert store.summarize_scoped(task, actor()).review_count == 1
    for other in (
        actor(tenant_id="foreign"),
        actor(project_id="foreign"),
        actor(data_domains=("other",)),
    ):
        with pytest.raises(PermissionError):
            store.summarize_scoped(task, other)
        with pytest.raises(PermissionError):
            store.submit_scoped(review(task), other)
        with pytest.raises(PermissionError):
            store.register_task(task, other)
    with pytest.raises(PermissionError, match="identity"):
        store.submit_scoped(review(task, "forged"), actor())
    with pytest.raises(PermissionError, match="task_access_denied"):
        store.summarize_scoped(uuid4(), actor())


def test_concurrent_duplicate_reviews_are_atomic(tmp_path):
    path = tmp_path / "reviews.db"
    task = uuid4()
    ScopedHumanReviewStore(path).register_task(task, actor())

    def submit(_):
        try:
            ScopedHumanReviewStore(path).submit_scoped(review(task), actor())
            return True
        except DuplicateReviewError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(submit, range(16))) == 1


def test_secured_review_api_checks_session_owner_and_domains(tmp_path):
    broker = SessionBroker()
    store = ScopedHumanReviewStore(tmp_path / "reviews.db")
    task = uuid4()
    store.register_task(task, actor())
    client = TestClient(create_app(session_broker=broker, review_store=store, production_mode=True))

    def headers(user="reviewer", tenant="t", project="p", domain="commerce"):
        token = broker.issue(
            user_id=user, tenant_id=tenant, project_id=project, data_domains=frozenset({domain})
        )
        return {
            "authorization": f"Bearer {token}",
            "x-user-id": user,
            "x-tenant-id": tenant,
            "x-project-id": project,
            "x-data-domain": domain,
        }

    for scope in ({"tenant": "other"}, {"project": "other"}, {"domain": "other"}):
        assert (
            client.get(f"/v1/reviews/{task}/summary", headers=headers(**scope)).status_code == 403
        )
        assert (
            client.post(
                "/v1/reviews", headers=headers(**scope), json=review(task).model_dump(mode="json")
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/v1/reviews", headers=headers(), json=review(task, "forged").model_dump(mode="json")
        ).status_code
        == 403
    )
    for user in ("reviewer", "second"):
        assert (
            client.post(
                "/v1/reviews",
                headers=headers(user=user),
                json=review(task, user).model_dump(mode="json"),
            ).status_code
            == 200
        )
    assert client.get(f"/v1/reviews/{task}/summary", headers=headers()).json()["passed"] is True
    assert (
        client.post(
            "/v1/reviews", headers=headers(), json=review(task).model_dump(mode="json")
        ).status_code
        == 409
    )
