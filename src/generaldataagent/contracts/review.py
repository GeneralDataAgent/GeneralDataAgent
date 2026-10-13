"""Contracts for the Stage 1 human review gate."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from generaldataagent.contracts.models import ContractModel, utc_now


class HumanReview(ContractModel):
    task_id: UUID
    reviewer_id: str = Field(min_length=1, max_length=128)
    semantic_score: int = Field(ge=1, le=5)
    conclusion_score: int = Field(ge=1, le=5)
    chart_score: int = Field(ge=1, le=5)
    limitations_score: int = Field(ge=1, le=5)
    blocking_issue: bool = False
    notes: str = Field(default="", max_length=4_000)
    evidence_uris: tuple[str, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def average_score(self) -> float:
        return round(
            (
                self.semantic_score
                + self.conclusion_score
                + self.chart_score
                + self.limitations_score
            )
            / 4,
            2,
        )


class HumanReviewSummary(ContractModel):
    task_id: UUID
    review_count: int = Field(ge=0)
    average_score: float = Field(ge=0, le=5)
    minimum_component_score: int | None = Field(default=None, ge=1, le=5)
    blocking_issue_count: int = Field(ge=0)
    passed: bool
    required_reviews: int = Field(default=2, ge=2)
