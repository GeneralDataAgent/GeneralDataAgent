from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.contracts import HumanReview
from generaldataagent.evaluation import DuplicateReviewError, LocalHumanReviewStore


def review(task_id, reviewer_id, *, score=5, blocking_issue=False) -> HumanReview:
    return HumanReview(
        task_id=task_id,
        reviewer_id=reviewer_id,
        semantic_score=score,
        conclusion_score=score,
        chart_score=score,
        limitations_score=score,
        blocking_issue=blocking_issue,
        evidence_uris=(f"artifact://local/{task_id}/result.json",),
    )


def test_dual_human_review_gate_passes_only_after_two_reviews(tmp_path) -> None:
    store = LocalHumanReviewStore(tmp_path / "reviews.jsonl")
    task_id = uuid4()
    store.submit(review(task_id, "reviewer-a", score=4))
    assert store.summarize(task_id).passed is False

    store.submit(review(task_id, "reviewer-b", score=5))
    summary = store.summarize(task_id)
    assert summary.review_count == 2
    assert summary.average_score == 4.5
    assert summary.passed is True


def test_human_review_gate_rejects_duplicate_reviewer_and_blocker(tmp_path) -> None:
    store = LocalHumanReviewStore(tmp_path / "reviews.jsonl")
    task_id = uuid4()
    store.submit(review(task_id, "reviewer-a"))
    with pytest.raises(DuplicateReviewError, match="already reviewed"):
        store.submit(review(task_id, "reviewer-a"))

    store.submit(review(task_id, "reviewer-b", blocking_issue=True))
    assert store.summarize(task_id).passed is False


def test_human_review_api_records_and_summarizes(tmp_path) -> None:
    task_id = uuid4()
    client = TestClient(create_app(review_store=LocalHumanReviewStore(tmp_path / "reviews.jsonl")))
    for reviewer_id in ("reviewer-a", "reviewer-b"):
        response = client.post(
            "/v1/reviews",
            json=review(task_id, reviewer_id, score=4).model_dump(mode="json"),
        )
        assert response.status_code == 200

    response = client.get(f"/v1/reviews/{task_id}/summary")
    assert response.status_code == 200
    assert response.json()["passed"] is True
