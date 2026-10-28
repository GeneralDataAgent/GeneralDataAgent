"""Pin the receipt contract's stated invariants before any consumer adopts it.

This contract is proposed, not admitted. These tests describe what a receipt
must refuse; they are not evidence that any capability emits one yet.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from generaldataagent.contracts import (
    ArtifactRef,
    ExecutionMethod,
    ExecutionReceipt,
    ObservedUsage,
    ReceiptVerification,
    SourceBinding,
)

HASH = "sha256:" + "a" * 64
TASK = str(uuid4())


def artifact(task_id=TASK, uri="artifact://result/1", created_by="rule-workflow"):
    return ArtifactRef(
        task_id=task_id,
        uri=uri,
        artifact_type="rule.workflow.evidence",
        content_hash=HASH,
        created_by=created_by,
        size_bytes=10,
    )


def receipt(**changes):
    values = dict(
        task_id=TASK,
        action="rules.execute",
        actor_id="analyst",
        tenant_id="t",
        project_id="p",
        trace_id="trace-1",
        sources=(SourceBinding(source_id="annex", content_hash=HASH),),
        method=ExecutionMethod(method_id="rules.workflow", method_version="v1"),
        result_refs=(artifact(),),
        completeness="complete",
        verification=ReceiptVerification(
            scope="authoritative_rederivation", passed=True, check_codes=("fee_arithmetic",)
        ),
    )
    return ExecutionReceipt(**(values | changes))


def test_a_minimal_receipt_round_trips_with_its_version():
    value = receipt()
    assert value.receipt_version == "1.0"
    restored = ExecutionReceipt.model_validate_json(value.model_dump_json())
    assert restored == value


def test_unknown_usage_stays_unknown_instead_of_zero():
    usage = ObservedUsage()
    assert usage.input_tokens is None and usage.total_tokens is None
    assert usage.cost_usd is None
    assert usage.telemetry_complete is False


def test_complete_telemetry_requires_observed_tokens():
    with pytest.raises(ValidationError, match="complete telemetry"):
        ObservedUsage(input_tokens=10, telemetry_complete=True)
    assert ObservedUsage(input_tokens=10, output_tokens=2, total_tokens=12, telemetry_complete=True)


def test_partial_measurement_is_not_a_complete_total():
    usage = ObservedUsage(input_tokens=10, telemetry_complete=False)
    assert usage.total_tokens is None


def test_cross_task_reference_is_refused():
    with pytest.raises(ValidationError, match="receipt's task"):
        receipt(result_refs=(artifact(task_id=str(uuid4())),))


def test_mixed_task_references_are_refused():
    with pytest.raises(ValidationError, match="receipt's task"):
        receipt(result_refs=(artifact(), artifact(task_id=str(uuid4()), uri="artifact://other/2")))


def test_duplicate_result_references_are_refused():
    with pytest.raises(ValidationError, match="unique"):
        receipt(result_refs=(artifact(), artifact()))


def test_duplicate_source_identifiers_are_refused():
    with pytest.raises(ValidationError, match="source identifiers"):
        receipt(sources=(SourceBinding(source_id="annex", content_hash=HASH),) * 2)


def test_a_complete_execution_must_reference_its_result():
    with pytest.raises(ValidationError, match="complete execution"):
        receipt(result_refs=())


def test_changed_bytes_change_the_binding_not_its_meaning():
    first = receipt()
    second = receipt(sources=(SourceBinding(source_id="annex", content_hash="sha256:" + "b" * 64),))
    assert first.sources != second.sources


@pytest.mark.parametrize(
    "scope", ["producer_self_check", "authoritative_rederivation", "independent_implementation"]
)
def test_verification_scope_is_preserved_rather_than_collapsed(scope):
    value = ReceiptVerification(scope=scope, passed=True, check_codes=("c",), verifier="reviewer-1")
    assert value.scope == scope


def test_an_unverified_execution_cannot_claim_a_pass():
    with pytest.raises(ValidationError, match="unverified"):
        ReceiptVerification(scope="none", passed=True)


def test_a_declared_scope_needs_named_checks():
    with pytest.raises(ValidationError, match="named checks"):
        ReceiptVerification(scope="authoritative_rederivation", passed=True)


@pytest.mark.parametrize("scope", ["independent_implementation", "human_attestation"])
def test_stronger_scopes_require_a_named_verifier(scope):
    with pytest.raises(ValidationError, match="named verifier"):
        ReceiptVerification(scope=scope, passed=True, check_codes=("c",))
    assert (
        ReceiptVerification(
            scope=scope, passed=True, check_codes=("c",), verifier="reviewer-1"
        ).verifier
        == "reviewer-1"
    )


def test_a_producer_self_check_needs_no_separate_verifier():
    assert ReceiptVerification(scope="producer_self_check", passed=False, check_codes=("c",))


def test_receipts_are_immutable():
    value = receipt()
    with pytest.raises(ValidationError):
        value.task_id = str(uuid4())


def test_extra_fields_are_refused():
    with pytest.raises(ValidationError):
        ExecutionReceipt(**{**receipt().model_dump(), "unexpected": 1})


def test_receipt_does_not_claim_authenticity():
    """A digest is byte identity; the contract exposes no origin or approval claim."""
    fields = set(ExecutionReceipt.model_fields)
    assert not fields & {"authentic", "approved", "signed_by", "reviewer_identity"}


def test_a_stored_receipt_reads_back_exactly_as_written():
    import json

    from generaldataagent.contracts import load_receipt

    stored = json.loads(receipt().model_dump_json())
    before = json.dumps(stored, sort_keys=True)
    loaded = load_receipt(stored)
    assert loaded.model_dump(mode="json") == stored
    # Reading is not an upgrade: the stored object is untouched.
    assert json.dumps(stored, sort_keys=True) == before


def test_a_receipt_from_another_version_is_refused_by_name():
    from generaldataagent.contracts import ReceiptVersionUnsupported, load_receipt

    stored = receipt().model_dump(mode="json")
    with pytest.raises(ReceiptVersionUnsupported) as error:
        load_receipt(stored | {"receipt_version": "1.1"})
    assert "1.1" in str(error.value) and "1.0" in str(error.value)


def test_a_stored_receipt_must_state_its_version():
    from generaldataagent.contracts import load_receipt

    stored = receipt().model_dump(mode="json")
    del stored["receipt_version"]
    # Constructing a receipt defaults the version; reading a stored one must not.
    with pytest.raises(ValueError, match="must state its receipt_version"):
        load_receipt(stored)


@pytest.mark.parametrize("payload", ["[1, 2]", "not json", 7])
def test_a_payload_that_is_not_an_object_is_refused(payload):
    from generaldataagent.contracts import load_receipt

    with pytest.raises(ValueError, match="JSON object|not valid JSON"):
        load_receipt(payload)


def test_reading_a_receipt_does_not_relabel_a_newer_version():
    from generaldataagent.contracts import ReceiptVersionUnsupported, load_receipt

    stored = receipt().model_dump(mode="json")
    # A newer receipt may carry fields this contract does not know; dropping them
    # silently would report a different execution as the same one.
    with pytest.raises(ReceiptVersionUnsupported):
        load_receipt(stored | {"receipt_version": "2.0", "future_field": {"x": 1}})
