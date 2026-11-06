"""Fail-closed deterministic verifiers for versioned evidence rubrics.

The evaluator consumes a ``TaskRubric``, a ``DeliveryPackage``, named source
bindings and an ``ExecutionReceipt``. It re-derives the rubric and receipt
digests, checks that every declared verifier is registered, then runs each
verifier against the exact task-bound evidence. It performs no model calls, no
repairs, no writes and no retries: a missing input, unknown verifier, cross-task
reference or unverifiable source is a gap. ``needs_human`` never becomes ``pass``.
"""

from collections.abc import Callable
from datetime import date
from uuid import UUID

from generaldataagent.contracts.delivery import DeliveryPackage
from generaldataagent.contracts.models import ArtifactRef
from generaldataagent.contracts.receipt import ExecutionReceipt, SourceBinding
from generaldataagent.contracts.rubric import (
    RUBRIC_VERSION,
    RubricCheckResult,
    RubricEvaluation,
    RubricOutcome,
    RubricSourceRequirement,
    TaskRubric,
    evaluation_payload,
    rubric_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash

__all__ = [
    "RubricRefused",
    "evaluate_rubric",
    "registered_verifiers",
]

_REQUIRED_VERIFIER = "rubric.required_checks_present"
_BASELINE_VERIFIERS = (
    "rubric.time_range_bound",
    "rubric.metric_version_bound",
    "rubric.claims_grounded",
)
_VERIFIERS: dict[str, Callable[..., RubricCheckResult]] = {}


class RubricRefused(ValueError):
    """The rubric cannot be evaluated without weakening its own acceptance rules."""


def _register(verifier_id: str):
    def wrap(fn):
        _VERIFIERS[verifier_id] = fn
        return fn

    return wrap


def registered_verifiers() -> tuple[str, ...]:
    return tuple(sorted(_VERIFIERS))


def _check(
    check_id: str,
    verifier_id: str,
    outcome: RubricOutcome,
    code: str,
    message: str,
    *,
    evidence_digest: str | None = None,
) -> RubricCheckResult:
    return RubricCheckResult(
        check_id=check_id,
        verifier_id=verifier_id,
        outcome=outcome,
        code=code,
        message=message,
        evidence_digest=evidence_digest,
    )


@_register(_REQUIRED_VERIFIER)
def _required_checks(rubric, package, sources, receipt):
    declared = set(rubric.delivery.verifier_ids)
    unregistered = declared - set(_VERIFIERS)
    missing_baseline = [value for value in _BASELINE_VERIFIERS if value not in declared]
    if _REQUIRED_VERIFIER not in declared or unregistered or missing_baseline:
        return _check(
            "a-required-checks",
            _REQUIRED_VERIFIER,
            "gap",
            "required_checks_missing",
            "Every registered verifier and baseline check is required; "
            f"unregistered={sorted(unregistered)}, missing={missing_baseline}.",
        )
    return _check(
        "a-required-checks",
        _REQUIRED_VERIFIER,
        "satisfied",
        "all_required_verifiers_registered",
        "Every declared verifier is registered and the baseline checks are present.",
    )


@_register("rubric.time_range_bound")
def _time_range(rubric, package, sources, receipt):
    verifier_id = "rubric.time_range_bound"
    try:
        start, end = date.fromisoformat(rubric.start_time), date.fromisoformat(rubric.end_time)
    except ValueError:
        return _check(
            "b-time-range",
            verifier_id,
            "gap",
            "time_range_unparseable",
            "Rubric time range must use ISO dates.",
        )
    if start >= end:
        return _check(
            "b-time-range",
            verifier_id,
            "gap",
            "time_range_inverted",
            "Rubric time range is inverted.",
        )
    # A delivery without a declared time-range claim cannot prove the requested period.
    claim_text = " ".join(claim.text for claim in package.claims)
    if rubric.start_time not in claim_text or rubric.end_time not in claim_text:
        return _check(
            "b-time-range",
            verifier_id,
            "gap",
            "time_range_not_claimed",
            "The delivery does not state the rubric's exact time range.",
        )
    return _check(
        "b-time-range",
        verifier_id,
        "satisfied",
        "time_range_bound",
        "The delivery states the rubric's exact ISO time range.",
    )


@_register("rubric.metric_version_bound")
def _metric_version(rubric, package, sources, receipt):
    verifier_id = "rubric.metric_version_bound"
    evidence_digest = canonical_hash(
        {
            "metric": package.metric,
            "rubric_metric": rubric.metric_name,
            "version": rubric.metric_version,
            "method": rubric.method.model_dump(mode="json"),
            "receipt_method": receipt.method.model_dump(mode="json"),
        }
    )
    if (
        package.metric != rubric.metric_name
        or rubric.method.method_id != receipt.method.method_id
        or rubric.method.method_version != receipt.method.method_version
    ):
        return _check(
            "c-metric-version",
            verifier_id,
            "gap",
            "method_mismatch",
            "The delivery metric or receipt method does not match the rubric.",
            evidence_digest=evidence_digest,
        )
    if receipt.verification.scope == "none" or not receipt.verification.passed:
        return _check(
            "c-metric-version",
            verifier_id,
            "gap",
            "method_unverified",
            "The receipt cannot attest to the declared method.",
            evidence_digest=evidence_digest,
        )
    return _check(
        "c-metric-version",
        verifier_id,
        "satisfied",
        "method_bound",
        "The metric and method version are bound to a passed receipt.",
        evidence_digest=evidence_digest,
    )


@_register("rubric.sources_single_snapshot")
def _sources(rubric, package, sources, receipt):
    verifier_id = "rubric.sources_single_snapshot"
    observed = {item.source_id: item for item in sources}
    receipt_sources = {item.source_id: item for item in receipt.sources}
    declared_source_ids = {item.source_id for item in rubric.sources}
    mismatches = []
    for required in rubric.sources:
        found = observed.get(required.source_id)
        if (
            found is None
            or found.content_hash != required.content_hash
            or found.version != required.version
            or receipt_sources.get(required.source_id) != found
        ):
            mismatches.append(required.source_id)
    extra_sources = sorted(set(observed) - declared_source_ids)
    extra_receipt_sources = sorted(set(receipt_sources) - declared_source_ids)
    evidence_digest = canonical_hash(
        {
            "sources": [item.model_dump(mode="json") for item in sources],
            "result_refs": [ref.model_dump(mode="json") for ref in receipt.result_refs],
        }
    )
    if (
        mismatches
        or extra_sources
        or extra_receipt_sources
        or len(observed) != len(sources)
        or len(receipt_sources) != len(receipt.sources)
    ):
        return _check(
            "d-sources",
            verifier_id,
            "gap",
            "source_snapshot_mismatch",
            f"Source snapshot mismatch: {sorted(mismatches)}, extra={extra_sources}.",
            evidence_digest=evidence_digest,
        )
    if any(ref.task_id != rubric.task_id for ref in receipt.result_refs):
        return _check(
            "d-sources",
            verifier_id,
            "gap",
            "cross_task_result_ref",
            "Receipt result references belong to another task.",
            evidence_digest=evidence_digest,
        )
    return _check(
        "d-sources",
        verifier_id,
        "satisfied",
        "single_source_snapshot",
        "All declared sources and receipt result references are bound to this task.",
        evidence_digest=evidence_digest,
    )


@_register("rubric.claims_grounded")
def _claims(rubric, package, sources, receipt):
    verifier_id = "rubric.claims_grounded"
    evidence_uris = set(_evidence_bindings(rubric.task_id, package, receipt))
    missing_refs = sorted(
        {uri for claim in package.claims for uri in claim.evidence_uris} - evidence_uris
    )
    claim_types = {claim.claim_type for claim in package.claims}
    missing_types = sorted(set(rubric.delivery.required_claim_types) - claim_types)
    if missing_refs or missing_types or any(not claim.evidence_uris for claim in package.claims):
        return _check(
            "e-claims",
            verifier_id,
            "gap",
            "claims_not_grounded",
            f"Ungrounded claims: refs={missing_refs}, types={missing_types}.",
        )
    return _check(
        "e-claims",
        verifier_id,
        "satisfied",
        "all_claims_grounded",
        "Every claim cites same-task delivery evidence and required claim types are present.",
    )


@_register("rubric.table_and_chart_evidence")
def _table_chart(rubric, package, sources, receipt):
    verifier_id = "rubric.table_and_chart_evidence"
    evidence = _evidence_bindings(rubric.task_id, package, receipt)
    problems = []
    if package.table.data_artifact_uri not in evidence:
        problems.append("table")
    if package.chart.data_artifact_uri not in evidence:
        problems.append("chart")
    if problems:
        return _check(
            "f-table-chart",
            verifier_id,
            "gap",
            "table_chart_evidence_missing",
            f"Missing same-task evidence for: {problems}.",
        )
    return _check(
        "f-table-chart",
        verifier_id,
        "satisfied",
        "table_chart_evidence_present",
        "Table and chart reference same-task delivery evidence.",
    )


@_register("rubric.human_items_acknowledged")
def _human_items(rubric, package, sources, receipt):
    verifier_id = "rubric.human_items_acknowledged"
    if not rubric.delivery.human_confirmation_items:
        return _check(
            "g-human-items",
            verifier_id,
            "satisfied",
            "no_human_items",
            "No rubric item requires human confirmation.",
        )
    missing_items = [
        item
        for item in rubric.delivery.human_confirmation_items
        if item not in receipt.verification.check_codes
    ]
    if receipt.verification.scope != "human_attestation" or receipt.verification.verifier is None:
        return _check(
            "g-human-items",
            verifier_id,
            "needs_human",
            "human_attestation_required",
            "Rubric human-confirmation items require a named human attestation.",
        )
    if missing_items:
        return _check(
            "g-human-items",
            verifier_id,
            "needs_human",
            "human_attestation_item_missing",
            f"The named human attestation does not cover: {missing_items}.",
        )
    return _check(
        "g-human-items",
        verifier_id,
        "satisfied",
        "human_attestation_present",
        "A named human attestation covers every rubric human-confirmation item.",
    )


def _evidence_bindings(
    task_id: UUID,
    package: DeliveryPackage,
    receipt: ExecutionReceipt,
) -> dict[str, ArtifactRef]:
    evidence: dict[str, ArtifactRef] = {}
    for ref in package.evidence:
        if ref.task_id != task_id:
            continue
        existing = evidence.get(ref.uri)
        if existing is not None and existing != ref:
            raise RubricRefused("rubric_evidence_binding_conflict")
        evidence[ref.uri] = ref
    for ref in receipt.result_refs:
        if ref.task_id == task_id and ref.uri in evidence and evidence[ref.uri] != ref:
            raise RubricRefused("rubric_evidence_binding_conflict")
    return evidence


def _validate_inputs(
    rubric: TaskRubric,
    package: DeliveryPackage,
    sources: tuple[SourceBinding, ...],
    receipt: ExecutionReceipt,
) -> None:
    if rubric.schema_version != RUBRIC_VERSION:
        raise RubricRefused("rubric_version_unsupported")
    payload = rubric_payload(
        rubric_id=rubric.rubric_id,
        task_id=rubric.task_id,
        metric_name=rubric.metric_name,
        metric_version=rubric.metric_version,
        start_time=rubric.start_time,
        end_time=rubric.end_time,
        sources=rubric.sources,
        method=rubric.method,
        delivery=rubric.delivery,
    )
    if rubric.rubric_digest != canonical_hash(payload):
        raise RubricRefused("rubric_digest_mismatch")
    if set(rubric.delivery.verifier_ids) != set(_VERIFIERS):
        raise RubricRefused("rubric_verifier_set_mismatch")
    if receipt.task_id != str(rubric.task_id):
        raise RubricRefused("rubric_receipt_task_mismatch")
    if any(ref.task_id != rubric.task_id for ref in package.evidence):
        raise RubricRefused("rubric_evidence_task_mismatch")
    if (
        not isinstance(sources, tuple)
        or any(not isinstance(item, SourceBinding) for item in sources)
        or len({item.source_id for item in sources}) != len(sources)
    ):
        raise RubricRefused("rubric_duplicate_source_binding")
    if any(not isinstance(item, RubricSourceRequirement) for item in rubric.sources):
        raise RubricRefused("rubric_source_requirement_invalid")
    if any(item.verifier_id != "rubric.sources_single_snapshot" for item in rubric.sources):
        raise RubricRefused("rubric_source_verifier_mismatch")
    if rubric.method.verifier_id != "rubric.metric_version_bound":
        raise RubricRefused("rubric_method_verifier_mismatch")
    delivery_uris = {item.uri for item in package.evidence}
    if any(ref.uri not in delivery_uris for ref in receipt.result_refs):
        raise RubricRefused("rubric_result_refs_not_delivery_evidence")
    _evidence_bindings(rubric.task_id, package, receipt)


def evaluate_rubric(
    *,
    rubric: TaskRubric,
    package: DeliveryPackage,
    sources: tuple[SourceBinding, ...],
    receipt: ExecutionReceipt,
) -> RubricEvaluation:
    """Evaluate one exact rubric; never repair, upgrade or reinterpret evidence."""

    _validate_inputs(rubric, package, sources, receipt)
    results = [
        _VERIFIERS[verifier_id](rubric, package, sources, receipt)
        for verifier_id in rubric.delivery.verifier_ids
    ]
    checks = tuple(sorted(results, key=lambda item: item.check_id))
    gaps = any(item.outcome == "gap" for item in checks)
    humans = any(item.outcome == "needs_human" for item in checks)
    verdict = "fail" if gaps else "needs_human" if humans else "pass"
    payload = evaluation_payload(
        task_id=rubric.task_id,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.rubric_digest,
        checks=checks,
    )
    return RubricEvaluation(
        task_id=rubric.task_id,
        rubric_id=rubric.rubric_id,
        rubric_digest=rubric.rubric_digest,
        verdict=verdict,
        checks=checks,
        evaluation_digest=canonical_hash(payload),
    )
