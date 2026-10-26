"""Rule receipts preserve checkpoint provenance and the strength of the check that ran."""

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_stage9_governed import ACTOR, request, runner

from generaldataagent.business.rules.governed import _receipt_verification
from generaldataagent.contracts import ArtifactRef, ExecutionReceipt
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer


def test_rule_receipt_binds_identity_sources_method_and_verified_evidence(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        proposal = request()
        result = service.invoke(proposal, trace_id="receipt-trace")
        receipt = ExecutionReceipt.model_validate(result["receipt"])
        ref = ArtifactRef.model_validate(result["evidence"])
        evidence = json.loads(service.artifacts.read_bytes(ref))
        assert receipt.task_id == str(proposal.task_id)
        assert (receipt.actor_id, receipt.tenant_id, receipt.project_id) == ("u", "t", "p")
        assert receipt.trace_id == "receipt-trace"
        assert receipt.action == "rules.execute"
        assert receipt.method.method_id == "rules.workflow"
        assert receipt.method.method_version == "test-v1"
        assert receipt.method.runtime_fingerprint is None
        assert receipt.result_refs == (ref,)
        assert {source.source_id: source.content_hash for source in receipt.sources} == evidence[
            "source_hashes"
        ]
        assert receipt.completeness == "complete"
        assert receipt.verification.scope == "authoritative_rederivation"
        assert receipt.verification.passed is True
        assert receipt.verification.check_codes == tuple(evidence["verification"]["checks"])
        assert receipt.verification.verifier is None
        assert receipt.usage.total_tokens is None
        assert receipt.usage.tool_calls is None
        assert receipt.usage.telemetry_complete is False
        assert result["production_accepted"] is False


def flow_with_verifier(service, saver, verify, *, scope="producer_self_check"):
    from test_stage9_rule_workflow import services

    from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

    ports = replace(services({}), verify=verify, verification_scope=scope)
    return RuleWorkflowRuntime(
        services=ports,
        checkpointer=saver,
        source_hashes=service.runtime.source_hashes,
        workflow_version="test-v1",
    )


@pytest.mark.parametrize("passed", [True, False])
def test_generic_services_keep_self_check_strength_and_failure_outcome(tmp_path, passed):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        service.runtime = flow_with_verifier(
            service,
            saver,
            lambda _: {
                "passed": passed,
                "failures": [] if passed else ["fee_mismatch"],
                "checks": ["fee_check"],
                "scope": "independent_implementation",
            },
        )
        result = service.invoke(request(), trace_id="self-check")
        receipt = ExecutionReceipt.model_validate(result["receipt"])
        assert receipt.verification.scope == "producer_self_check"
        assert receipt.verification.passed is passed
        assert receipt.completeness == ("complete" if passed else "partial")
        evidence = json.loads(service.artifacts.read_bytes(receipt.result_refs[0]))
        assert evidence["verification"]["failures"] == ([] if passed else ["fee_mismatch"])
        assert (evidence["delivery"] is not None) is passed


def test_missing_named_checks_cannot_claim_verification(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        service.runtime = flow_with_verifier(
            service, saver, lambda _: {"passed": True, "failures": []}
        )
        result = service.invoke(request(), trace_id="unnamed")
        receipt = ExecutionReceipt.model_validate(result["receipt"])
        assert receipt.verification.scope == "none"
        assert receipt.verification.passed is False


def test_resume_keeps_original_verification_scope_without_replaying_verifier(tmp_path):
    calls = []
    proposal = request()
    parsed = RuleAnalysisRequest.model_validate(proposal.arguments)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)

        def verify(_):
            calls.append("verify")
            return {"passed": True, "failures": [], "checks": ["self_check"]}

        service.runtime = flow_with_verifier(service, saver, verify)
        service.runtime.run(
            parsed, ACTOR, thread_id=str(proposal.task_id), interrupt_after=("verify",)
        )
        service.runtime = flow_with_verifier(
            service, saver, verify, scope="authoritative_rederivation"
        )
        result = service.invoke(
            proposal.model_copy(update={"action": "resume"}), trace_id="resumed"
        )
        receipt = ExecutionReceipt.model_validate(result["receipt"])
        assert receipt.verification.scope == "producer_self_check"
        assert receipt.trace_id == "resumed"
        assert calls == ["verify"]


@pytest.mark.parametrize("change", ["source", "request", "version"])
def test_resume_binding_changes_cannot_publish_a_receipt(tmp_path, change):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        proposal = request()
        parsed = RuleAnalysisRequest.model_validate(proposal.arguments)
        service.runtime.run(parsed, ACTOR, thread_id=str(proposal.task_id))
        resumed = proposal.model_copy(update={"action": "resume"})
        if change == "source":
            service.runtime.source_hashes = lambda: {"annex": "sha256:" + "b" * 64}
        elif change == "version":
            service.runtime.workflow_version = "changed-v2"
        else:
            resumed = resumed.model_copy(
                update={
                    "arguments": parsed.model_copy(update={"transaction_value": "99"}).model_dump()
                }
            )
        with pytest.raises(ValueError, match="binding_changed"):
            service.invoke(resumed, trace_id="changed")
        assert not list(service.artifacts.root.iterdir())


def test_cross_task_evidence_reference_cannot_be_adopted(tmp_path, monkeypatch):
    from uuid import uuid4

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        original = service.artifacts.put_json

        def other_task(**arguments):
            return original(**{**arguments, "task_id": uuid4()})

        monkeypatch.setattr(service.artifacts, "put_json", other_task)
        with pytest.raises(ValidationError, match="receipt's task"):
            service.invoke(request(), trace_id="cross-task")


def test_corrupted_evidence_bytes_prevent_receipt_return(tmp_path, monkeypatch):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        original = service.artifacts.put_json

        def corrupt(**arguments):
            ref = original(**arguments)
            path = service.artifacts.root / ref.uri.removeprefix("artifact://local/")
            path.write_bytes(b"corrupted evidence")
            return ref

        monkeypatch.setattr(service.artifacts, "put_json", corrupt)
        with pytest.raises(ValueError, match="content hash mismatch"):
            service.invoke(request(), trace_id="corrupted")


def test_receipt_uses_composed_task_and_trace_with_governed_storage(tmp_path):
    from test_execution_composition import ACTOR as PRODUCT_ACTOR
    from test_execution_composition import TASK, product_composition, proposal, runtime

    from generaldataagent.business.rules.governed import GovernedRuleRunner
    from generaldataagent.contracts import Budget

    composition = product_composition(tmp_path, task_id=TASK, trace_id="composed-trace")
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = GovernedRuleRunner(
            runtime=runtime(tmp_path, saver),
            actor=PRODUCT_ACTOR,
            artifacts=composition.artifacts,
            audit=composition.audit,
            composition=composition,
        )
        composition.budgets.register(TASK, PRODUCT_ACTOR, Budget())
        result = service.invoke(proposal(task_id=TASK), trace_id="caller-trace")
        receipt = ExecutionReceipt.model_validate(result["receipt"])
        assert receipt.task_id == str(TASK)
        assert receipt.trace_id == "composed-trace"
        assert composition.artifacts.read_bytes(receipt.result_refs[0])


def test_legacy_verification_is_conservatively_a_self_check():
    result = _receipt_verification(
        {"passed": True, "checks": ["legacy_check"], "scope": "authoritative_rederivation"}
    )
    assert result.scope == "producer_self_check"
    assert result.passed is True


@pytest.mark.parametrize("scope", ["authoritative_rederivation", "legacy-arbitrary-label"])
def test_legacy_checkpoint_scope_metadata_cannot_upgrade_receipt(tmp_path, scope):
    from langgraph.graph import END, START, StateGraph

    from generaldataagent.orchestration.rule_workflow import RuleWorkflowState

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        proposal = request()
        parsed = RuleAnalysisRequest.model_validate(proposal.arguments)
        thread = str(proposal.task_id)
        _, state = service.runtime.run(parsed, ACTOR, thread_id=thread)
        # The pre-adoption graph had no wrapper-owned verification_scope channel.
        legacy = {key: value for key, value in state.items() if key != "verification_scope"}
        legacy["verification"] = {**legacy["verification"], "scope": scope}
        schema = dict(RuleWorkflowState.__annotations__)
        schema.pop("verification_scope")
        from typing import TypedDict

        old_state = TypedDict("LegacyRuleState", schema, total=False)
        graph = StateGraph(old_state)
        graph.add_node("legacy", lambda _: legacy)
        graph.add_edge(START, "legacy")
        graph.add_edge("legacy", END)
        graph.compile(checkpointer=saver).invoke(legacy, {"configurable": {"thread_id": thread}})
        result = service.invoke(proposal.model_copy(update={"action": "resume"}), trace_id="legacy")
        receipt = ExecutionReceipt.model_validate(result["receipt"])
        assert receipt.verification.scope == "producer_self_check"
        assert receipt.verification.passed is True


def test_legacy_source_names_receive_stable_collision_safe_receipt_identifiers(tmp_path):
    import hashlib

    unusual = "Annex.xlsx"
    alias = "s" + hashlib.sha256(unusual.encode()).hexdigest()[:63]
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        hashes = {unusual: "sha256:" + "a" * 64, alias: "sha256:" + "b" * 64}
        service.runtime.source_hashes = lambda: hashes
        proposal = request()
        first = service.invoke(proposal, trace_id="named-sources")
        resumed = service.invoke(
            proposal.model_copy(update={"action": "resume"}), trace_id="named-sources-resumed"
        )
        receipt = ExecutionReceipt.model_validate(first["receipt"])
        assert receipt.sources == ExecutionReceipt.model_validate(resumed["receipt"]).sources
        assert len({source.source_id for source in receipt.sources}) == 2
        assert {source.content_hash for source in receipt.sources} == set(hashes.values())
        evidence = json.loads(service.artifacts.read_bytes(receipt.result_refs[0]))
        assert evidence["source_hashes"] == hashes


@pytest.mark.parametrize("checks", ["not-a-list", [""], [1]])
def test_malformed_check_names_are_refused(checks):
    with pytest.raises(ValueError, match="checks_invalid"):
        _receipt_verification({"passed": True, "checks": checks})
