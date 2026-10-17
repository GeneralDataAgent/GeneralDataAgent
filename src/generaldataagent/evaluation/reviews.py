"""Append-only local human review ledger for development and offline acceptance."""

from pathlib import Path
from threading import Lock
from uuid import UUID

from generaldataagent.contracts import HumanReview, HumanReviewSummary


class DuplicateReviewError(ValueError):
    pass


class LocalHumanReviewStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def submit(self, review: HumanReview) -> HumanReview:
        with self._lock:
            existing = self.list_for_task(review.task_id)
            if any(item.reviewer_id == review.reviewer_id for item in existing):
                raise DuplicateReviewError(
                    f"reviewer {review.reviewer_id!r} already reviewed task {review.task_id}"
                )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(review.model_dump_json() + "\n")
        return review

    def list_for_task(self, task_id: UUID) -> tuple[HumanReview, ...]:
        if not self.path.exists():
            return ()
        reviews = (
            HumanReview.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        return tuple(item for item in reviews if item.task_id == task_id)

    def summarize(self, task_id: UUID, *, required_reviews: int = 2) -> HumanReviewSummary:
        reviews = self.list_for_task(task_id)
        return summarize_reviews(task_id, reviews, required_reviews=required_reviews)


def summarize_reviews(
    task_id: UUID, reviews: tuple[HumanReview, ...], *, required_reviews: int = 2
) -> HumanReviewSummary:
    if required_reviews < 2:
        raise ValueError("at least two reviews are required")
    component_scores = [
        score
        for item in reviews
        for score in (
            item.semantic_score,
            item.conclusion_score,
            item.chart_score,
            item.limitations_score,
        )
    ]
    average = (
        round(sum(item.average_score for item in reviews) / len(reviews), 2) if reviews else 0.0
    )
    minimum = min(component_scores) if component_scores else None
    blockers = sum(item.blocking_issue for item in reviews)
    passed = (
        len(reviews) >= required_reviews
        and average >= 4.0
        and minimum is not None
        and minimum > 1
        and blockers == 0
    )
    return HumanReviewSummary(
        task_id=task_id,
        review_count=len(reviews),
        average_score=average,
        minimum_component_score=minimum,
        blocking_issue_count=blockers,
        passed=passed,
        required_reviews=required_reviews,
    )
