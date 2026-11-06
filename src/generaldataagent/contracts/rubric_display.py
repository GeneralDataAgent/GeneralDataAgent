"""Deterministic user-facing view of one rubric evaluation.

Stage 14.1D groups the seven fixed verifier outcomes into the three states a
reader acts on: satisfied, gap and needs_human. It also states, without
inference, which provenance actually exists:

* the deterministic verifier run is the producer's own self-check;
* an independent recalculation is ``not_run`` — this slice has no such evidence;
* human signoff is ``required``/``satisfied``/``not_required`` according to the
  rubric's named human items and the attestation verifier.

The display accepts no model text. A model comment may be rendered beside it as
non-authoritative annotation, but it cannot enter this contract or change a
state. The digest binds every grouped check and the provenance declaration.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.contracts.rubric import RubricOutcome, RubricVerdict
from generaldataagent.memory.display_preferences import canonical_hash

DISPLAY_VERSION = "1.0"

HumanSignoff = Literal["satisfied", "required", "not_required"]
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class RubricDisplayItem(ContractModel):
    """One verifier result, preserved exactly as the evaluation produced it."""

    schema_version: Literal["1.0"] = "1.0"
    status: RubricOutcome
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    verifier_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    evidence_digest: str | None = Field(default=None, pattern=_DIGEST)


class RubricProvenance(ContractModel):
    """What is actually known, without upgrading self-check to independent truth."""

    schema_version: Literal["1.0"] = "1.0"
    producer_self_check: Literal[True] = True
    independent_recheck: Literal["not_run"] = "not_run"
    human_signoff: HumanSignoff
    human_items: tuple[str, ...] = Field(default=(), max_length=8)
    model_commentary_authoritative: Literal[False] = False

    @model_validator(mode="after")
    def consistent_signoff(self):
        if len(set(self.human_items)) != len(self.human_items):
            raise ValueError("rubric_display_duplicate_human_item")
        if (self.human_signoff == "not_required") != (len(self.human_items) == 0):
            raise ValueError("rubric_display_human_signoff_invalid")
        return self


class RubricDisplay(ContractModel):
    """Three explicit buckets, a fixed verdict and a digest-bound provenance."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    rubric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    rubric_digest: str = Field(pattern=_DIGEST)
    evaluation_digest: str = Field(pattern=_DIGEST)
    verdict: RubricVerdict
    check_count: int = Field(ge=1, le=16)
    satisfied: tuple[RubricDisplayItem, ...] = Field(default=(), max_length=16)
    gaps: tuple[RubricDisplayItem, ...] = Field(default=(), max_length=16)
    needs_human: tuple[RubricDisplayItem, ...] = Field(default=(), max_length=16)
    provenance: RubricProvenance
    display_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def consistent_display(self):
        groups = (
            ("satisfied", self.satisfied),
            ("gap", self.gaps),
            ("needs_human", self.needs_human),
        )
        seen: list[str] = []
        for status, items in groups:
            ids = [item.check_id for item in items]
            if ids != sorted(ids):
                raise ValueError("rubric_display_checks_unordered")
            if any(item.status != status for item in items):
                raise ValueError("rubric_display_status_mismatch")
            seen.extend(ids)
        if len(set(seen)) != len(seen):
            raise ValueError("rubric_display_duplicate_check")
        if self.check_count != len(seen):
            raise ValueError("rubric_display_check_count_mismatch")
        expected = "fail" if self.gaps else "needs_human" if self.needs_human else "pass"
        if self.verdict != expected:
            raise ValueError("rubric_display_verdict_invalid")
        payload = display_payload(
            task_id=self.task_id,
            rubric_id=self.rubric_id,
            rubric_digest=self.rubric_digest,
            evaluation_digest=self.evaluation_digest,
            verdict=self.verdict,
            satisfied=self.satisfied,
            gaps=self.gaps,
            needs_human=self.needs_human,
            check_count=self.check_count,
            provenance=self.provenance,
        )
        if self.display_digest != canonical_hash(payload):
            raise ValueError("rubric_display_digest_mismatch")
        return self


def _item_payload(item: RubricDisplayItem) -> dict:
    return {
        "status": item.status,
        "check_id": item.check_id,
        "verifier_id": item.verifier_id,
        "code": item.code,
        "message": item.message,
        "evidence_digest": item.evidence_digest,
    }


def display_payload(
    *,
    task_id: UUID,
    rubric_id: str,
    rubric_digest: str,
    evaluation_digest: str,
    verdict: RubricVerdict,
    satisfied: tuple[RubricDisplayItem, ...],
    gaps: tuple[RubricDisplayItem, ...],
    needs_human: tuple[RubricDisplayItem, ...],
    check_count: int,
    provenance: RubricProvenance,
) -> dict:
    """Canonical bytes bound by ``RubricDisplay.display_digest``."""

    return {
        "schema_version": DISPLAY_VERSION,
        "task_id": str(task_id),
        "rubric_id": rubric_id,
        "rubric_digest": rubric_digest,
        "evaluation_digest": evaluation_digest,
        "verdict": verdict,
        "check_count": check_count,
        "satisfied": [_item_payload(item) for item in satisfied],
        "gaps": [_item_payload(item) for item in gaps],
        "needs_human": [_item_payload(item) for item in needs_human],
        "provenance": {
            "producer_self_check": provenance.producer_self_check,
            "independent_recheck": provenance.independent_recheck,
            "human_signoff": provenance.human_signoff,
            "human_items": list(provenance.human_items),
            "model_commentary_authoritative": provenance.model_commentary_authoritative,
        },
    }
