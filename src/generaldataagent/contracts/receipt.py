"""Versioned execution receipt shared by rule and science capabilities.

A receipt records what was executed, against which inputs, by which method, and
with what verification scope. It is a description of an execution, not an
attestation: a content hash proves byte identity, never origin, reviewer
identity, independence, or correctness.

Unknown observations stay unknown. A receipt never substitutes zero for a
missing count, and a weaker verification is never relabelled as a stronger one.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ArtifactRef, ContractModel

RECEIPT_VERSION = "1.0"
SUPPORTED_RECEIPT_VERSIONS = frozenset({RECEIPT_VERSION})


class ReceiptVersionUnsupported(ValueError):
    """A stored receipt was written by a version this code does not implement."""


# Producer self-check, authoritative re-derivation by the same implementation,
# an independently implemented check, and a human attestation are different
# strengths. They are declared, never collapsed into one "verified" label.
VerificationScope = Literal[
    "producer_self_check",
    "authoritative_rederivation",
    "independent_implementation",
    "human_attestation",
    "none",
]


class SourceBinding(ContractModel):
    """One named input and the digest of the exact bytes that were read.

    ``source_id`` is a caller-assigned stable name (for example ``annex`` or
    ``payments``). The digest identifies content; it does not establish who
    produced it, whether it was approved, or that it is fresh.
    """

    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: str | None = Field(default=None, min_length=1, max_length=256)


class ExecutionMethod(ContractModel):
    """The implementation and runtime that performed the work."""

    schema_version: str = "1.1"
    method_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    method_version: str = Field(min_length=1, max_length=128)
    random_seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    runtime_fingerprint: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Digest of the executing runtime, when one is identifiable.",
    )


class ObservedUsage(ContractModel):
    """Measured resource use, with completeness stated rather than implied.

    A missing observation is ``None`` and ``telemetry_complete`` is false. A
    partial measurement is reported as measured, never as a complete total.
    """

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    telemetry_complete: bool = False

    @model_validator(mode="after")
    def completeness_matches_observations(self) -> ObservedUsage:
        if self.telemetry_complete and (
            self.input_tokens is None or self.output_tokens is None or self.total_tokens is None
        ):
            raise ValueError("complete telemetry requires observed token counts")
        return self


class ReceiptVerification(ContractModel):
    """What was checked, by which strength, with a stable outcome code."""

    scope: VerificationScope
    passed: bool
    check_codes: tuple[str, ...] = ()
    verifier: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def scope_matches_outcome(self) -> ReceiptVerification:
        if self.scope == "none" and self.passed:
            raise ValueError("an unverified execution cannot report a pass")
        if self.scope != "none" and not self.check_codes:
            raise ValueError("a declared verification scope requires named checks")
        if self.scope in {"independent_implementation", "human_attestation"} and (
            self.verifier is None
        ):
            raise ValueError("independent or human verification requires a named verifier")
        return self


class ExecutionReceipt(ContractModel):
    """One completed execution, bound to its task and its exact inputs.

    ``result_refs`` must belong to the same task as the receipt. A cross-task
    reference is refused at construction rather than at a later consumer, so a
    receipt cannot launder another task's evidence into this one.
    """

    receipt_version: Literal["1.0"] = RECEIPT_VERSION
    task_id: str = Field(min_length=1, max_length=128)
    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    actor_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)

    sources: tuple[SourceBinding, ...] = ()
    method: ExecutionMethod
    result_refs: tuple[ArtifactRef, ...] = ()
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    usage: ObservedUsage = Field(default_factory=ObservedUsage)
    verification: ReceiptVerification

    @model_validator(mode="after")
    def references_are_same_task_and_unique(self) -> ExecutionReceipt:
        task_ids = {str(ref.task_id) for ref in self.result_refs}
        if task_ids and task_ids != {self.task_id}:
            raise ValueError("receipt result references must belong to the receipt's task")
        uris = [ref.uri for ref in self.result_refs]
        if len(set(uris)) != len(uris):
            raise ValueError("receipt result references must be unique")
        source_ids = [source.source_id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("receipt source identifiers must be unique")
        if self.completeness == "complete" and not self.result_refs:
            raise ValueError("a complete execution must reference its result artifacts")
        return self


def load_receipt(payload: str | bytes | dict) -> ExecutionReceipt:
    """Read a stored receipt as written: no defaulting, no upgrade, no rewrite.

    A stored receipt is evidence of what a producer wrote. Reading it must not
    assume a version it never stated, and it must not accept a version this code
    does not implement by quietly ignoring the extra fields. Both are refused by
    name, so a consumer can say "written by a newer version" rather than report a
    generic validation failure — or worse, fold a newer receipt into an older
    reading and call the result the same thing.
    """
    if isinstance(payload, (str, bytes)):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError("receipt is not valid JSON") from None
    elif isinstance(payload, dict):
        data = payload
    else:
        raise ValueError("a receipt must be a JSON object")
    if not isinstance(data, dict):
        raise ValueError("a receipt must be a JSON object")
    if "receipt_version" not in data:
        raise ValueError("a stored receipt must state its receipt_version")
    version = data["receipt_version"]
    if version not in SUPPORTED_RECEIPT_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_RECEIPT_VERSIONS))
        raise ReceiptVersionUnsupported(
            f"receipt version {version!r} is not supported; this code reads {supported}"
        )
    return ExecutionReceipt.model_validate(data)


__all__ = [
    "ExecutionMethod",
    "ExecutionReceipt",
    "ObservedUsage",
    "ReceiptVerification",
    "ReceiptVersionUnsupported",
    "SUPPORTED_RECEIPT_VERSIONS",
    "SourceBinding",
    "VerificationScope",
    "load_receipt",
]
