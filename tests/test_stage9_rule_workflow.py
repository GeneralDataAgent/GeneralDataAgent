from dataclasses import replace

import pytest

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime, RuleWorkflowServices

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
REQUEST = RuleAnalysisRequest(operation="select", semantics_excerpt="null means all")
SOURCES = {"annex": "sha256:" + "a" * 64}


def services(counts, *, passed=True):
    def node(name, value):
        def execute(state):
            counts[name] = counts.get(name, 0) + 1
            return value

        return execute

    return RuleWorkflowServices(
        monthly_context=node("monthly_context", {"volume": "100.00"}),
        match_rules=node("match_rules", {"ids": ["A"]}),
        compute=node("compute", {"fee": "0.064"}),
        verify=node("verify", {"passed": passed, "failures": [] if passed else ["fee_mismatch"]}),
        deliver=node("deliver", {"answer": "0.064"}),
    )


def runtime(saver, ports, sources=lambda: SOURCES, version="synthetic-v1"):
    return RuleWorkflowRuntime(
        services=ports, checkpointer=saver, source_hashes=sources, workflow_version=version
    )


@pytest.mark.parametrize("stage", ["monthly_context", "match_rules", "compute", "verify"])
def test_rule_resume_after_restart_does_not_replay_completed_nodes(tmp_path, stage):
    counts = {}
    ports = services(counts)
    path = tmp_path / "checkpoint.sqlite"
    with open_protected_checkpointer(path, encryption_key=b"k" * 32) as saver:
        thread, _ = runtime(saver, ports).run(REQUEST, ACTOR, interrupt_after=(stage,))
        completed = dict(counts)
    with open_protected_checkpointer(path, encryption_key=b"k" * 32) as saver:
        result = runtime(saver, ports).resume(thread, REQUEST, ACTOR)
        assert result["status"] == "completed"
        assert all(counts[name] == count for name, count in completed.items())
        assert set(counts.values()) == {1}
    assert b"0.064" not in path.read_bytes()


@pytest.mark.parametrize("change", ["request", "source", "version", "actor"])
def test_resume_rejects_changed_binding_before_running_a_node(tmp_path, change):
    counts = {}
    ports = services(counts)
    current = dict(SOURCES)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(saver, ports, sources=lambda: current)
        thread, _ = flow.run(REQUEST, ACTOR, interrupt_after=("compute",))
        before = dict(counts)
        request, actor = REQUEST, ACTOR
        if change == "request":
            request = RuleAnalysisRequest(
                operation="select", semantics_excerpt="null means all", account_type="R"
            )
        elif change == "source":
            current["annex"] = "sha256:" + "b" * 64
        elif change == "version":
            flow = runtime(saver, ports, version="synthetic-v2")
        else:
            actor = ACTOR.model_copy(update={"tenant_id": "other"})
        with pytest.raises((ValueError, PermissionError)):
            flow.resume(thread, request, actor)
        assert counts == before


def test_failed_verification_never_delivers(tmp_path):
    counts = {}
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(saver, services(counts, passed=False))
        thread, result = flow.run(REQUEST, ACTOR)
        assert result["status"] == "failed"
        assert "delivery" not in result and "deliver" not in counts
        assert flow.resume(thread, REQUEST, ACTOR)["status"] == "failed"
        assert counts["verify"] == 1


def test_invalid_verification_and_mid_node_source_change_fail_closed(tmp_path):
    counts = {}
    current = dict(SOURCES)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        ports = replace(services(counts), verify=lambda _: {"passed": True, "failures": ["bad"]})
        with pytest.raises(ValueError, match="verification_invalid"):
            runtime(saver, ports).run(REQUEST, ACTOR)
        assert "deliver" not in counts

        def mutate_source(state):
            current["annex"] = "sha256:" + "b" * 64
            return {"fee": "0.064"}

        ports = replace(services(counts), compute=mutate_source)
        with pytest.raises(ValueError, match="binding_changed"):
            runtime(saver, ports, sources=lambda: current).run(REQUEST, ACTOR)


def test_failed_compute_resumes_after_restart_with_real_annex_handlers(tmp_path):
    from uuid import uuid4

    from generaldataagent.business.rules.workflow import AnnexWorkflowServices

    annex = [
        dict(
            ID="A",
            card_scheme="Alpha",
            account_type=None,
            aci=None,
            merchant_category_code=None,
            fixed_amount="0.05",
            rate="14",
        )
    ]
    documents = "null means all\nfee = fixed_amount + rate * transaction_value / 10000"
    adapter = AnnexWorkflowServices(annex, documents=documents)
    ports = adapter.services()
    counts = {"compute": 0, "match": 0}
    original_match = ports.match_rules
    original_compute = ports.compute

    def match(state):
        counts["match"] += 1
        return original_match(state)

    def broken_compute(state):
        counts["compute"] += 1
        raise RuntimeError("synthetic transient compute failure")

    request = RuleAnalysisRequest(operation="average_fee", semantics_excerpt="null means all")
    thread = str(uuid4())
    path = tmp_path / "fault-checkpoint.sqlite"
    with open_protected_checkpointer(path, encryption_key=b"k" * 32) as saver:
        flow = runtime(
            saver,
            replace(ports, match_rules=match, compute=broken_compute),
            sources=adapter.source_hashes,
        )
        with pytest.raises(RuntimeError, match="transient"):
            flow.run(request, ACTOR, thread_id=thread)
    with open_protected_checkpointer(path, encryption_key=b"k" * 32) as saver:
        flow = runtime(
            saver,
            replace(ports, match_rules=match, compute=original_compute),
            sources=adapter.source_hashes,
        )
        result = flow.resume(thread, request, ACTOR)
        assert result["status"] == "completed"
        assert result["delivery"]["average_fee"] == "0.064"
        assert result["delivery"]["items"] == ["A"]
        assert counts == {"compute": 1, "match": 1}


@pytest.mark.parametrize("tamper", ["ids", "coefficient", "average"])
def test_real_annex_verifier_rejects_tampered_computation(tmp_path, tamper):
    from generaldataagent.business.rules.workflow import AnnexWorkflowServices

    adapter = AnnexWorkflowServices(
        [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")],
        documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
    )
    ports = adapter.services()

    def compute(state):
        result = ports.compute(state)
        if tamper == "ids":
            result["fees"] = []
        elif tamper == "coefficient":
            result["fees"][0].update(fixed_amount="1", fee="1.014")
        else:
            result["average_fee"] = "999"
        return result

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        flow = runtime(saver, replace(ports, compute=compute), sources=adapter.source_hashes)
        request = RuleAnalysisRequest(operation="average_fee", semantics_excerpt="null means all")
        _, result = flow.run(request, ACTOR)
        assert result["status"] == "failed"
        assert "delivery" not in result


def test_node_cannot_mutate_checkpoint_request(tmp_path):
    def mutate_copy(state):
        state["request"]["account_type"] = "unexpected"
        return {"volume": "100"}

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        ports = replace(services({}), monthly_context=mutate_copy)
        _, result = runtime(saver, ports).run(REQUEST, ACTOR)
        assert result["request"]["account_type"] is None
