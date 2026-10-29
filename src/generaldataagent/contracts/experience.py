"""Experience lessons and provenance shared by product and evaluation consumers."""

import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from generaldataagent.contracts.models import ContractModel


class ExperienceRecord(ContractModel):
    """One reusable lesson, without questions, answers or case identifiers."""

    experience_id: UUID = Field(default_factory=uuid4)
    failure_class: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    applies_when: tuple[str, ...] = Field(min_length=1, max_length=10)
    does_not_apply_when: tuple[str, ...] = Field(min_length=1, max_length=10)
    repair: str = Field(min_length=1, max_length=2000)
    counterexample: str = Field(min_length=1, max_length=2000)
    tool_scope: tuple[str, ...] = Field(min_length=1, max_length=20)
    owner: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime

    def text(self) -> str:
        """Everything a reviewer or the DLP scanner needs to see."""
        return json.dumps(
            {
                "failure_class": self.failure_class,
                "applies_when": list(self.applies_when),
                "does_not_apply_when": list(self.does_not_apply_when),
                "repair": self.repair,
                "counterexample": self.counterexample,
                "tool_scope": list(self.tool_scope),
            },
            sort_keys=True,
            ensure_ascii=False,
        )


class FeedbackSource(BaseModel):
    """Stored capture receipt; excluded from runtime lesson prompts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experience_id: UUID
    dataset_id: UUID
    dataset_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    feedback_id: UUID
    task_id: UUID
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    split: Literal["training"]


class ExtractionSource(BaseModel):
    """Stored extraction receipt; carries digests and steps, never trace prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experience_id: UUID
    template_version: str = Field(min_length=1, max_length=64)
    failure_step: int = Field(ge=1)
    recovery_step: int = Field(ge=1)
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    trace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    documents_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    extractor_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExtractedExperience(ContractModel):
    """Review provenance kept outside the runtime-searchable lesson."""

    template_version: str = "rule-excerpt-v1"
    failure_step: int = Field(ge=1)
    recovery_step: int = Field(ge=1)
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    record: ExperienceRecord


class SqlRepairEvidence(BaseModel):
    """An observed query repair, not proof of source authenticity or answer correctness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sql-repair.v1"] = "sql-repair.v1"
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    failure_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    failure_step: int = Field(ge=1, strict=True)
    recovery_step: int = Field(ge=1, strict=True)
    failed_query_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovered_query_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
