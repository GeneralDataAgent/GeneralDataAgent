"""Versioned ledger for bounded, rubric-bound revisions.

Stage 14.1C defines the *record* of a revision, not the revision itself. Each
version retains the exact input and output delivery digests together with both
evidence sets, so a later reader can replay the chain without trusting a model.
The ledger is bound to one rubric digest: changing the acceptance definition
creates a new ledger instead of rewriting this one.
"""

import re
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import canonical_hash

REVISION_VERSION = "1.0"
MAX_REVISIONS = 2

_DIGEST = r"^sha256:[0-9a-f]{64}$"
_DIGEST_RE = re.compile(_DIGEST)


def ordered_evidence(values: tuple[str, ...]) -> tuple[str, ...]:
    """Evidence digests are a set, written in one canonical order."""
    return tuple(sorted(set(values)))


class RevisionBudget(ContractModel):
    """The remaining resources a caller may spend on one more revision.

    Every component must stay strictly positive. A revision is refused when any
    of them is spent; the controller reports the first exhaustion in a fixed
    order (time, tokens, tool calls) so the stop reason is deterministic.
    """

    schema_version: Literal["1.0"] = "1.0"
    remaining_seconds: int = Field(ge=0)
    remaining_tokens: int = Field(ge=0)
    remaining_tool_calls: int = Field(ge=0)


class RevisionVersion(ContractModel):
    """One retained revision: exact input, exact output and both evidence sets."""

    schema_version: Literal["1.0"] = "1.0"
    attempt: int = Field(ge=1, le=MAX_REVISIONS)
    failure_code: str = Field(min_length=1, max_length=128)
    cause: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    input_digest: str = Field(pattern=_DIGEST)
    output_digest: str = Field(pattern=_DIGEST)
    input_evidence_digests: tuple[str, ...] = Field(default=(), max_length=32)
    output_evidence_digests: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def consistent_version(self):
        if self.input_digest == self.output_digest:
            raise ValueError("revision_output_unchanged")
        for values in (self.input_evidence_digests, self.output_evidence_digests):
            if values != ordered_evidence(values):
                raise ValueError("revision_evidence_unordered")
            if any(not _DIGEST_RE.match(value) for value in values):
                raise ValueError("revision_evidence_digest_invalid")
        return self


class RevisionLedger(ContractModel):
    """Immutable chain of at most two revisions against one rubric version."""

    schema_version: Literal["1.0"] = "1.0"
    rubric_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    task_id: UUID
    rubric_digest: str = Field(pattern=_DIGEST)
    initial_delivery_digest: str = Field(pattern=_DIGEST)
    initial_evidence_digests: tuple[str, ...] = Field(default=(), max_length=32)
    versions: tuple[RevisionVersion, ...] = Field(default=(), max_length=MAX_REVISIONS)
    revision_count: int = Field(ge=0, le=MAX_REVISIONS)
    stopped_reason: str = Field(pattern=r"^revision_[a-z0-9_]{1,96}$")
    ledger_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def consistent_ledger(self):
        if self.revision_count != len(self.versions):
            raise ValueError("revision_count_mismatch")
        if self.initial_evidence_digests != ordered_evidence(self.initial_evidence_digests):
            raise ValueError("revision_evidence_unordered")
        if any(not _DIGEST_RE.match(value) for value in self.initial_evidence_digests):
            raise ValueError("revision_evidence_digest_invalid")
        attempts = [item.attempt for item in self.versions]
        if attempts != list(range(1, len(self.versions) + 1)):
            raise ValueError("revision_attempt_sequence_invalid")
        previous_digest = self.initial_delivery_digest
        previous_evidence = self.initial_evidence_digests
        for item in self.versions:
            if item.input_digest != previous_digest:
                raise ValueError("revision_chain_broken")
            if item.input_evidence_digests != previous_evidence:
                raise ValueError("revision_evidence_chain_broken")
            previous_digest = item.output_digest
            previous_evidence = item.output_evidence_digests
        payload = ledger_payload(
            rubric_id=self.rubric_id,
            task_id=self.task_id,
            rubric_digest=self.rubric_digest,
            initial_delivery_digest=self.initial_delivery_digest,
            initial_evidence_digests=self.initial_evidence_digests,
            versions=self.versions,
            stopped_reason=self.stopped_reason,
        )
        if self.ledger_digest != canonical_hash(payload):
            raise ValueError("revision_ledger_digest_mismatch")
        return self


def ledger_payload(
    *,
    rubric_id: str,
    task_id: UUID,
    rubric_digest: str,
    initial_delivery_digest: str,
    initial_evidence_digests: tuple[str, ...],
    versions: tuple[RevisionVersion, ...],
    stopped_reason: str,
) -> dict:
    """Canonical bytes bound by ``RevisionLedger.ledger_digest``."""

    return {
        "schema_version": REVISION_VERSION,
        "rubric_id": rubric_id,
        "task_id": str(task_id),
        "rubric_digest": rubric_digest,
        "initial_delivery_digest": initial_delivery_digest,
        "initial_evidence_digests": list(initial_evidence_digests),
        "versions": [
            {
                "attempt": item.attempt,
                "failure_code": item.failure_code,
                "cause": item.cause,
                "input_digest": item.input_digest,
                "output_digest": item.output_digest,
                "input_evidence_digests": list(item.input_evidence_digests),
                "output_evidence_digests": list(item.output_evidence_digests),
            }
            for item in versions
        ],
        "stopped_reason": stopped_reason,
    }
