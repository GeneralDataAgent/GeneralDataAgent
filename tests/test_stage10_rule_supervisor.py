import json
from uuid import uuid4

import pytest

from generaldataagent.agents.rule_supervisor import (
    RULE_ROLE_SEQUENCE,
    RULE_SUPERVISOR_PROMPT,
    RuleSupervisorRuntime,
)
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.governance import FeedbackRegistry
from generaldataagent.business.rules.delivery import RuleDeliveryRuntime
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext, DeliveryPackage
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
DOCUMENTS = "null means all\nfee = fixed_amount + rate * transaction_value / 10000"
ANNEX = [
    dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14"),
    dict(ID="B", card_scheme="Beta", fixed_amount="0.10", rate="20"),
]


def request(**changes):
    values = dict(operation="average_fee", semantics_excerpt="null means all")
    return RuleAnalysisRequest(**(values | changes))


def build(tmp_path, saver, *, request_value=None, governed=False, tamper=None):
    adapter = AnnexWorkflowServices(ANNEX, documents=DOCUMENTS)
    ports = adapter.services()
    if tamper is not None:
        original = ports.compute
        ports = ports.__class__(
            monthly_context=ports.monthly_context,
            match_rules=ports.match_rules,
            compute=lambda state: tamper(original(state)),
            verify=ports.verify,
            deliver=ports.deliver,
        )
    flow = RuleWorkflowRuntime(
        services=ports,
        checkpointer=saver,
        source_hashes=adapter.source_hashes,
        workflow_version="supervisor-v1",
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")
    delivery = RuleDeliveryRuntime(
        workflow=flow,
        artifacts=artifacts,
        feedback=FeedbackRegistry(tmp_path / "feedback.sqlite"),
        audit=audit,
    )
    runner = (
        GovernedRuleRunner(runtime=flow, actor=ACTOR, artifacts=artifacts, audit=audit)
        if governed
        else None
    )
    return RuleSupervisorRuntime(
        model=None,
        request=request_value or request(),
        workflow=flow,
        delivery=delivery,
        task_id=uuid4(),
        runner=runner,
    )


def fake_builder(script):
    """Capture the tool groups and drive them with a scripted role sequence."""

    class FakeSupervisor:
        def __init__(self, groups):
            self.groups = groups

        async def ainvoke(self, state, config):
            script(self.groups)
            return {"structured_response": None}

    built = []

    def builder(**kwargs):
        assert kwargs["system_prompt"] == RULE_SUPERVISOR_PROMPT
        assert "MUST delegate" in kwargs["system_prompt"]
        supervisor = FakeSupervisor(kwargs["tool_groups"])
        built.append(supervisor)
        return supervisor

    return builder, built


def run(tmp_path, monkeypatch, script, *, governed=False, tamper=None, request_value=None):
    builder, built = fake_builder(script)
    monkeypatch.setattr("generaldataagent.agents.rule_supervisor.build_supervisor", builder)
    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(
            tmp_path,
            saver,
            request_value=request_value,
            governed=governed,
            tamper=tamper,
        )
        result = runtime.run(ACTOR, thread_id=str(runtime.task_id))
    return runtime, result, built


def test_full_role_sequence_returns_a_packaged_delivery(tmp_path, monkeypatch):
    def script(groups):
        groups["discovery"][0].invoke({})
        groups["data_analysis"][0].invoke({})
        groups["verification"][0].invoke({})
        groups["reporting"][0].invoke({})

    runtime, result, built = run(tmp_path, monkeypatch, script)
    assert result.status == "completed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE
    assert result.task_id == str(runtime.task_id)
    assert result.artifact_uris and all(uri for uri in result.artifact_uris)
    assert any("not an independent semantic oracle" in line for line in result.limitations)
    # The delivered package is the same shape the metric path produces.
    package = DeliveryPackage.model_validate(built[0].groups["reporting"][0].invoke({}))
    assert package.metric == "rule:average_fee"
    assert package.row_count == 1


def test_reporter_runs_deterministically_when_the_model_omits_it(tmp_path, monkeypatch):
    def script(groups):
        groups["discovery"][0].invoke({})
        groups["data_analysis"][0].invoke({})
        groups["verification"][0].invoke({})

    _, result, _ = run(tmp_path, monkeypatch, script)
    assert result.status == "completed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE


def test_out_of_order_roles_are_refused(tmp_path, monkeypatch):
    def script(groups):
        with pytest.raises(PermissionError, match="expected 'discovery'"):
            groups["verification"][0].invoke({})

    _, result, _ = run(tmp_path, monkeypatch, script)
    assert result.status == "failed"
    assert result.delegated_roles == ()
    assert result.limitations[1] == "actual role sequence: none"


def test_reporter_refuses_an_unverified_analysis(tmp_path, monkeypatch):
    refusals = []

    def script(groups):
        groups["discovery"][0].invoke({})
        groups["data_analysis"][0].invoke({})
        groups["verification"][0].invoke({})
        try:
            groups["reporting"][0].invoke({})
        except PermissionError as exc:
            refusals.append(str(exc))

    _, result, _ = run(
        tmp_path, monkeypatch, script, tamper=lambda result: result | {"average_fee": "999"}
    )
    # The reporter refused, so its role was not recorded and no package exists.
    assert refusals == ["rule analysis has not passed verification"]
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:3]
    assert result.status == "failed"
    assert result.limitations[0].startswith("required role sequence:")
    assert result.artifact_uris == ()


@pytest.mark.parametrize("arguments", [{"operation": "select"}, {"merchant": "Shop"}])
def test_bound_tools_reject_any_argument(tmp_path, monkeypatch, arguments):
    def script(groups):
        with pytest.raises(ValueError):
            groups["discovery"][0].invoke(arguments)

    _, result, _ = run(tmp_path, monkeypatch, script)
    assert result.status == "failed"


def test_governed_runner_puts_execution_and_delivery_through_policy_audit(tmp_path, monkeypatch):
    def script(groups):
        groups["discovery"][0].invoke({})
        groups["data_analysis"][0].invoke({})
        groups["verification"][0].invoke({})
        groups["reporting"][0].invoke({})

    runtime, result, _ = run(tmp_path, monkeypatch, script, governed=True)
    assert result.status == "completed"
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    # rules.execute, artifact.write (evidence) and rules.deliver are all audited.
    assert [row["payload"]["decision"] for row in rows] == ["allow"] * len(rows)
    assert len(rows) >= 3
    assert all(row["task_id"] == str(runtime.task_id) for row in rows)


def test_verifier_reads_the_checkpoint_rather_than_the_cached_copy(tmp_path, monkeypatch):
    seen = {}

    def script(groups):
        groups["discovery"][0].invoke({})
        groups["data_analysis"][0].invoke({})
        seen["verification"] = groups["verification"][0].invoke({})

    _, result, _ = run(tmp_path, monkeypatch, script)
    assert seen["verification"]["status"] == "pass"
    assert seen["verification"]["failures"] == []
    assert result.status == "completed"


def test_discovery_describes_the_bound_request_without_leaking_values(tmp_path, monkeypatch):
    seen = {}

    def script(groups):
        seen["discovery"] = groups["discovery"][0].invoke({})

    _, result, _ = run(
        tmp_path,
        monkeypatch,
        script,
        request_value=request(operation="applicable", merchant="Shop", year=2023, month=1),
    )
    # The summary describes the shape of the request and the bound source, never
    # a result value. Source identity is the hash binding, not a registry lookup.
    assert seen["discovery"] == {
        "operation": "applicable",
        "requires_merchant_context": True,
        "time_scope": "month",
        "source_hashes": seen["discovery"]["source_hashes"],
        "data_domains": ["payments"],
        "execution_contract": (
            "Only execute through execute_rule_analysis; do not compute or restate values."
        ),
    }
    assert set(seen["discovery"]["source_hashes"]) == {"annex", "documents"}
    assert all(value.startswith("sha256:") for value in seen["discovery"]["source_hashes"].values())
    # No payments source is wired, so the contextual run fails closed.
    assert result.status == "failed"


def test_runtime_rejects_unvalidated_requests():
    with pytest.raises(ValueError, match="validated analysis request"):
        RuleSupervisorRuntime(
            model=None,
            request={"operation": "average_fee"},
            workflow=None,
            delivery=None,
            task_id=uuid4(),
        )


def test_runtime_requires_a_task_id():
    with pytest.raises(ValueError, match="task id"):
        RuleSupervisorRuntime(
            model=None, request=request(), workflow=None, delivery=None, task_id="not-a-uuid"
        )


def test_actor_scope_is_required_before_any_tool_runs(tmp_path):
    from generaldataagent.contracts import ActorContext as Scope

    with open_protected_checkpointer(tmp_path / "state", encryption_key=b"k" * 32) as saver:
        runtime = build(tmp_path, saver)
        # ActorContext forbids an empty domain tuple, so the guard is exercised
        # through a scope object that was not validated by the contract.
        scope = Scope.model_construct(user_id="u", tenant_id="t", project_id="p", data_domains=())
        groups = runtime._build_actor_tools(scope, {}, [])
        with pytest.raises(PermissionError, match="authorized data domain"):
            groups["discovery"][0].invoke({})


def test_the_analyst_tool_returns_the_computed_answer(tmp_path, monkeypatch):
    """A careful model refuses to report a result it never saw.

    The metric path returns rows from its executor; the rule path must do the
    same, and the value must be the workflow's, not one the model supplied.
    """
    seen = {}

    def script(groups):
        groups["discovery"][0].invoke({})
        seen["analysis"] = groups["data_analysis"][0].invoke({})

    _, result, _ = run(tmp_path, monkeypatch, script)
    assert seen["analysis"]["verification_passed"] is True
    label, value = seen["analysis"]["answer"][0]
    assert label == "average_fee"
    # The fixture rules charge 0.05+14*10/10000 and 0.10+20*10/10000, so the
    # workflow's average is 0.092. This is the engine's number, not the model's.
    assert float(value) == pytest.approx(0.092)
    # Without the verifier the role sequence is incomplete; the answer alone
    # never counts as a completed run.
    assert result.status == "failed"
    assert result.delegated_roles == RULE_ROLE_SEQUENCE[:2]


def test_the_analyst_tool_returns_no_answer_for_a_failed_run(tmp_path, monkeypatch):
    seen = {}

    def script(groups):
        groups["discovery"][0].invoke({})
        seen["analysis"] = groups["data_analysis"][0].invoke({})

    _, result, _ = run(
        tmp_path, monkeypatch, script, tamper=lambda result: result | {"average_fee": "999"}
    )
    assert seen["analysis"]["verification_passed"] is False
    assert seen["analysis"]["answer"] is None
    assert result.status == "failed"
