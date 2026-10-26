"""Changed sources fork a task and reuse only context with proven equal inputs."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_stage9_governed import ACTOR, request, runner
from test_stage9_rule_workflow import REQUEST, services

from generaldataagent.contracts import ArtifactRef
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime


def setup_flow(saver, current, counts, *, dependencies=("documents", "payments"), version="v1"):
    return RuleWorkflowRuntime(
        services=replace(services(counts), context_sources=dependencies),
        checkpointer=saver,
        source_hashes=lambda: dict(current),
        workflow_version=version,
    )


def hashes():
    return {
        name: "sha256:" + letter * 64
        for name, letter in (("annex", "a"), ("documents", "b"), ("payments", "c"))
    }


@pytest.mark.parametrize("changed", ["annex", "documents", "payments"])
def test_changed_source_reruns_only_proven_safe_prefix(tmp_path, changed):
    current, counts = hashes(), {}
    path = tmp_path / "state"
    with open_protected_checkpointer(path, encryption_key=b"k" * 32) as saver:
        flow = setup_flow(saver, current, counts)
        parent, original = flow.run(REQUEST, ACTOR)
    current[changed] = "sha256:" + "d" * 64
    with open_protected_checkpointer(path, encryption_key=b"k" * 32) as saver:
        flow = setup_flow(saver, current, counts)
        with pytest.raises(ValueError, match="binding_changed"):
            flow.resume(parent, REQUEST, ACTOR)
        child, state = flow.recompute(parent, REQUEST, ACTOR)
        assert child != parent and state["parent_thread_id"] == parent
        assert state["status"] == "completed"
        assert state["source_hashes"] == current
        assert counts["monthly_context"] == (1 if changed == "annex" else 2)
        assert all(counts[name] == 2 for name in ("match_rules", "compute", "verify", "deliver"))
        assert state["reused_stages"] == (["monthly_context"] if changed == "annex" else [])
        with saver.execution(parent, ACTOR):
            assert (
                dict(flow.graph.get_state({"configurable": {"thread_id": parent}}).values)
                == original
            )
        assert flow.resume(child, REQUEST, ACTOR) == state


@pytest.mark.parametrize("dependency", [None, ("annex", "documents", "payments")])
def test_unknown_or_changed_dependency_declaration_forces_full_recompute(tmp_path, dependency):
    current, counts = hashes(), {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        parent, _ = setup_flow(saver, current, counts).run(REQUEST, ACTOR)
        current["annex"] = "sha256:" + "d" * 64
        flow = setup_flow(saver, current, counts, dependencies=dependency)
        _, state = flow.recompute(parent, REQUEST, ACTOR)
        assert state["reused_stages"] == []
        assert counts["monthly_context"] == 2


def test_checkpoint_without_dependency_fingerprint_recomputes_every_stage(tmp_path):
    current, counts = hashes(), {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        parent, original = setup_flow(saver, current, counts, dependencies=None).run(REQUEST, ACTOR)
        assert "context_input_hash" not in original
        current["annex"] = "sha256:" + "d" * 64
        _, state = setup_flow(saver, current, counts).recompute(parent, REQUEST, ACTOR)
        assert state["reused_stages"] == []
        assert all(value == 2 for value in counts.values())


def test_recompute_rejects_interrupted_parent_and_reused_task_id(tmp_path):
    current, counts = hashes(), {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = setup_flow(saver, current, counts)
        parent, _ = flow.run(REQUEST, ACTOR, interrupt_after=("compute",))
        with pytest.raises(ValueError, match="recompute_parent_invalid"):
            flow.recompute(parent, REQUEST, ACTOR)
        original = flow.resume(parent, REQUEST, ACTOR)
        before = dict(counts)
        with pytest.raises(PermissionError, match="thread_already_registered"):
            flow.recompute(parent, REQUEST, ACTOR, thread_id=parent)
        assert counts == before
        assert flow.state_of(parent, REQUEST, ACTOR) == original


@pytest.mark.parametrize("change", ["request", "actor", "domains", "version"])
def test_recompute_rejects_changed_execution_identity(tmp_path, change):
    current, counts = hashes(), {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = setup_flow(saver, current, counts)
        parent, _ = flow.run(REQUEST, ACTOR)
        proposal, actor = REQUEST, ACTOR
        if change == "request":
            proposal = REQUEST.model_copy(update={"transaction_value": "999"})
        elif change == "actor":
            actor = ACTOR.model_copy(update={"tenant_id": "other"})
        elif change == "domains":
            actor = ACTOR.model_copy(update={"data_domains": (*ACTOR.data_domains, "extra")})
        else:
            flow = setup_flow(saver, current, counts, version="v2")
        before = dict(counts)
        with pytest.raises((ValueError, PermissionError)):
            flow.recompute(parent, proposal, actor)
        assert counts == before


def test_failed_reverification_never_reuses_previous_delivery(tmp_path):
    current, counts = hashes(), {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = setup_flow(saver, current, counts)
        parent, _ = flow.run(REQUEST, ACTOR)
        current["annex"] = "sha256:" + "d" * 64
        failing = RuleWorkflowRuntime(
            services=replace(
                services(counts, passed=False), context_sources=("documents", "payments")
            ),
            checkpointer=saver,
            source_hashes=lambda: current,
            workflow_version="v1",
        )
        _, state = failing.recompute(parent, REQUEST, ACTOR)
        assert state["status"] == "failed"
        assert "delivery" not in state and counts["deliver"] == 1
        assert counts["verify"] == 2 and counts["monthly_context"] == 1


def test_source_change_during_recompute_fails_closed(tmp_path):
    current, counts = hashes(), {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = setup_flow(saver, current, counts)
        parent, _ = flow.run(REQUEST, ACTOR)
        ports = services(counts)

        def mutate(state):
            current["payments"] = "sha256:" + "e" * 64
            return ports.compute(state)

        changed = RuleWorkflowRuntime(
            services=replace(ports, compute=mutate, context_sources=("documents", "payments")),
            checkpointer=saver,
            source_hashes=lambda: current,
            workflow_version="v1",
        )
        with pytest.raises(ValueError, match="binding_changed"):
            changed.recompute(parent, REQUEST, ACTOR)
        assert counts["verify"] == 1 and counts["deliver"] == 1


def test_real_annex_change_reuses_context_but_recomputes_new_fee(tmp_path):
    from generaldataagent.business.rules.workflow import AnnexWorkflowServices

    def adapter(rate):
        return AnnexWorkflowServices(
            [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate=rate)],
            documents="null means all\\nfee = fixed_amount + rate * transaction_value / 10000",
        )

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        old = adapter("14")
        request_value = RuleAnalysisRequest(
            operation="average_fee", semantics_excerpt="null means all"
        )
        flow = RuleWorkflowRuntime(
            services=old.services(),
            checkpointer=saver,
            source_hashes=old.source_hashes,
            workflow_version="annex-v1",
        )
        parent, first = flow.run(request_value, ACTOR)
        new = adapter("20")
        changed = RuleWorkflowRuntime(
            services=new.services(),
            checkpointer=saver,
            source_hashes=new.source_hashes,
            workflow_version="annex-v1",
        )
        _, state = changed.recompute(parent, request_value, ACTOR)
        assert first["delivery"]["average_fee"] == "0.064"
        assert state["delivery"]["average_fee"] == "0.07"
        assert state["reused_stages"] == ["monthly_context"]
        assert state["verification"]["passed"] is True


def test_governed_recompute_keeps_old_evidence_and_emits_new_task_receipt(tmp_path):
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        service = runner(saver, tmp_path)
        first = request()
        original = service.invoke(first, trace_id="original")
        old_ref = ArtifactRef.model_validate(original["evidence"])
        old_bytes = service.artifacts.read_bytes(old_ref)
        sources = dict(service.runtime.source_hashes())
        sources["annex"] = "sha256:" + "e" * 64
        service.runtime.source_hashes = lambda: sources
        child = first.model_copy(
            update={
                "task_id": uuid4(),
                "action": "recompute",
                "arguments": {"parent_thread_id": str(first.task_id), "request": first.arguments},
            }
        )
        result = service.invoke(child, trace_id="recomputed")
        ref = ArtifactRef.model_validate(result["evidence"])
        evidence = json.loads(service.artifacts.read_bytes(ref))
        assert evidence["parent_thread_id"] == str(first.task_id)
        assert evidence["reused_stages"] == ["monthly_context"]
        assert result["receipt"]["task_id"] == str(child.task_id)
        assert ref.task_id == child.task_id
        assert {s["source_id"]: s["content_hash"] for s in result["receipt"]["sources"]} == sources
        assert service.artifacts.read_bytes(old_ref) == old_bytes
        assert (
            service.runtime.state_of(
                str(child.task_id), RuleAnalysisRequest.model_validate(first.arguments), ACTOR
            )["status"]
            == "completed"
        )
