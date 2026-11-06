"""Versioned, fail-closed evidence rubrics for a single delivery.

Stage 14.1A is a *declaration and evaluation* slice only. A rubric binds required
source/version/time/method facts, output shape, claim coverage and human-confirmation
items to named deterministic verifiers. The evaluator never calls a model, never
repairs a delivery and never upgrades an older or unknown rubric. Unknown verifiers,
missing required checks, wrong-task evidence, mixed data snapshots, missing time
ranges and failed checks all produce an explicit refusal rather than a partial pass.
"""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import canonical_hash

RUBRIC_VERSION = "1.0"

RubricOutcome = Literal["satisfied", "gap", "needs_human"]
RubricVerdict = Literal["pass", "fail", "needs_human"]

RUBRIC_VERIFIER_IDS = frozenset(
    {
        "rubric.required_checks_present",
        "rubric.time_range_bound",
        "rubric.metric_version_bound",
        "rubric.sources_single_snapshot",
        "rubric.claims_grounded",
        "rubric.table_and_chart_evidence",
        "rubric.human_items_acknowledged",
    }
)
_VERIFIER_IDS = RUBRIC_VERIFIER_IDS


class RubricSourceRequirement(ContractModel):
    """One named source and exact expected content hash."""

    schema_version: Literal["1.0"] = "1.0"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: str | None = Field(default=None, min_length=1, max_length=128)
    verifier_id: str = "rubric.sources_single_snapshot"


class RubricMethodRequirement(ContractModel):
    """Method identity plus the deterministic verifier that binds it."""

    schema_version: Literal["1.0"] = "1.0"
    method_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    method_version: str = Field(min_length=1, max_length=128)
    verifier_id: str = "rubric.metric_version_bound"


class RubricDeliveryRequirement(ContractModel):
    """Required claim, human-confirmation and verifier facts.

    v1 deliberately requires every registered verifier. A partial, duplicate or
    unknown list is refused at construction rather than silently skipping a check.
    Reordering is allowed; evaluation emits checks in canonical ``check_id`` order.
    """

    schema_version: Literal["1.0"] = "1.0"
    required_claim_types: tuple[str, ...] = Field(default=(), max_length=8)
    human_confirmation_items: tuple[str, ...] = Field(default=(), max_length=8)
    verifier_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def known_verifiers(self):
        if not set(self.verifier_ids) <= _VERIFIER_IDS:
            raise ValueError("rubric_unknown_verifier")
        if len(set(self.verifier_ids)) != len(self.verifier_ids):
            raise ValueError("rubric_duplicate_verifier")
        if set(self.verifier_ids) != _VERIFIER_IDS:
            raise ValueError("rubric_incomplete_verifier_set")
        if len(set(self.human_confirmation_items)) != len(self.human_confirmation_items):
            raise ValueError("rubric_duplicate_human_item")
        return self


class TaskRubric(ContractModel):
    """One immutable acceptance definition, bound to task, metric and sources."""

    schema_version: Literal["1.0"] = "1.0"
    rubric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    task_id: UUID
    metric_name: str = Field(min_length=1, max_length=128)
    metric_version: str = Field(pattern=r"^v[1-9][0-9]*$")
    start_time: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    end_time: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    sources: tuple[RubricSourceRequirement, ...] = Field(min_length=1, max_length=8)
    method: RubricMethodRequirement
    delivery: RubricDeliveryRequirement
    rubric_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent_rubric(self):
        ids = tuple(item.source_id for item in self.sources)
        if len(set(ids)) != len(ids):
            raise ValueError("rubric_duplicate_source")
        try:
            start, end = date.fromisoformat(self.start_time), date.fromisoformat(self.end_time)
        except ValueError:
            raise ValueError("rubric_time_range_unparseable") from None
        if start >= end:
            raise ValueError("rubric_inverted_time_range")
        payload = rubric_payload(
            rubric_id=self.rubric_id,
            task_id=self.task_id,
            metric_name=self.metric_name,
            metric_version=self.metric_version,
            start_time=self.start_time,
            end_time=self.end_time,
            sources=self.sources,
            method=self.method,
            delivery=self.delivery,
        )
        if self.rubric_digest != canonical_hash(payload):
            raise ValueError("rubric_digest_mismatch")
        return self


def rubric_payload(
    *,
    rubric_id: str,
    task_id: UUID,
    metric_name: str,
    metric_version: str,
    start_time: str,
    end_time: str,
    sources: tuple[RubricSourceRequirement, ...],
    method: RubricMethodRequirement,
    delivery: RubricDeliveryRequirement,
) -> dict:
    """Canonical bytes bound by ``TaskRubric.rubric_digest``."""

    return {
        "schema_version": RUBRIC_VERSION,
        "rubric_id": rubric_id,
        "task_id": str(task_id),
        "metric_name": metric_name,
        "metric_version": metric_version,
        "start_time": start_time,
        "end_time": end_time,
        "sources": [
            {
                "source_id": item.source_id,
                "content_hash": item.content_hash,
                "version": item.version,
                "verifier_id": item.verifier_id,
            }
            for item in sources
        ],
        "method": {
            "method_id": method.method_id,
            "method_version": method.method_version,
            "verifier_id": method.verifier_id,
        },
        "delivery": {
            "required_claim_types": list(delivery.required_claim_types),
            "human_confirmation_items": list(delivery.human_confirmation_items),
            "verifier_ids": list(delivery.verifier_ids),
        },
    }


class RubricCheckResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    verifier_id: str
    outcome: RubricOutcome
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class RubricEvaluation(ContractModel):
    """Deterministic evaluation of one delivery against one exact rubric version."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    rubric_id: str
    rubric_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verdict: RubricVerdict
    checks: tuple[RubricCheckResult, ...] = Field(min_length=1, max_length=16)
    producer_self_check_only: Literal[True] = True
    model_judgement_used: Literal[False] = False
    revision_performed: Literal[False] = False
    evaluation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent_evaluation(self):
        ids = tuple(item.check_id for item in self.checks)
        if len(set(ids)) != len(ids) or ids != tuple(sorted(ids)):
            raise ValueError("rubric_evaluation_checks_invalid")
        gaps = [item for item in self.checks if item.outcome == "gap"]
        humans = [item for item in self.checks if item.outcome == "needs_human"]
        expected = "fail" if gaps else "needs_human" if humans else "pass"
        if self.verdict != expected:
            raise ValueError("rubric_evaluation_verdict_invalid")
        if not set(item.verifier_id for item in self.checks) <= _VERIFIER_IDS:
            raise ValueError("rubric_evaluation_unknown_verifier")
        payload = evaluation_payload(
            task_id=self.task_id,
            rubric_id=self.rubric_id,
            rubric_digest=self.rubric_digest,
            checks=self.checks,
        )
        if self.evaluation_digest != canonical_hash(payload):
            raise ValueError("rubric_evaluation_digest_mismatch")
        return self


def evaluation_payload(
    *,
    task_id: UUID,
    rubric_id: str,
    rubric_digest: str,
    checks: tuple[RubricCheckResult, ...],
) -> dict:
    return {
        "schema_version": RUBRIC_VERSION,
        "task_id": str(task_id),
        "rubric_id": rubric_id,
        "rubric_digest": rubric_digest,
        "checks": [
            {
                "check_id": item.check_id,
                "verifier_id": item.verifier_id,
                "outcome": item.outcome,
                "code": item.code,
                "message": item.message,
                "evidence_digest": item.evidence_digest,
            }
            for item in checks
        ],
    }
