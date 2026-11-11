"""Durable question-task acceptance contracts and explicit failures."""

from typing import Literal

from pydantic import model_validator

from generaldataagent.contracts import MetricCandidate
from generaldataagent.contracts.models import ContractModel
from generaldataagent.orchestration.task_queue import MetricTaskRecord


class MetricQuestionTask(ContractModel):
    """Accepted question task bound to the one certified metric it resolved to."""

    schema_version: Literal["1.0"] = "1.0"
    task: MetricTaskRecord
    metric: MetricCandidate


class MetricQuestionRejection(ContractModel):
    """Explicit ambiguity or absence; the question body is never echoed here."""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["ambiguous", "not_found"]
    candidates: tuple[MetricCandidate, ...] = ()

    @model_validator(mode="after")
    def status_matches_candidates(self) -> "MetricQuestionRejection":
        if self.status == "ambiguous" and len(self.candidates) < 2:
            raise ValueError("ambiguous question requires multiple candidates")
        if self.status == "not_found" and self.candidates:
            raise ValueError("unresolved question cannot include candidates")
        return self


class QuestionAcceptanceUnavailable(RuntimeError):
    """No certified semantic catalog is wired to the task service."""


class QuestionNotResolved(Exception):
    """Question did not resolve to exactly one authorized certified metric."""

    def __init__(self, status: str, candidates: tuple[MetricCandidate, ...]) -> None:
        super().__init__("question_not_resolved")
        self.status = status
        self.candidates = candidates
