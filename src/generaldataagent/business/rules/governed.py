"""Development-only policy/audit/artifact adapter for rule workflow execution."""

from uuid import UUID

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore, require_artifact_scope
from generaldataagent.contracts import ActorContext, ToolRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy import PolicyEngine
from generaldataagent.tools.gateway import ToolGateway


class GovernedRuleRunner:
    """Route execution and evidence writes through existing gateway policy.

    An optional ``ExecutionComposition`` declares which guarantee set this run
    carries. ``product`` compositions make the gateway itself refuse to run
    without verified audit, task budgets and a governed, actor-scoped artifact
    store; a ``research`` composition keeps the development ports. Both are
    constructor checks rather than flags, and neither is an admission or an
    acceptance claim.

    A product composition does not register the task budget: the calling task
    owns its budget and must register it first. An unregistered task is refused
    by the gateway, which is the intended fail-closed outcome.
    """

    def __init__(
        self,
        *,
        runtime: RuleWorkflowRuntime,
        actor: ActorContext,
        artifacts: LocalArtifactStore | GovernedArtifactStore,
        audit: JsonlAuditSink | VerifiedAuditSink,
        policy: PolicyEngine | None = None,
        composition: ExecutionComposition | None = None,
    ):
        if not isinstance(artifacts, LocalArtifactStore | GovernedArtifactStore) or not isinstance(
            audit, JsonlAuditSink | VerifiedAuditSink
        ):
            raise ValueError("rule_runner_requires_artifact_store_and_audit")
        if composition is not None:
            # Check the guarantee itself before the wiring, so a development
            # audit sink in a product composition names the control it misses.
            if composition.mode == "product" and (
                not isinstance(artifacts, GovernedArtifactStore)
                or not isinstance(audit, VerifiedAuditSink)
            ):
                raise ValueError("rule_runner_product_composition_requires_governed_ports")
            if (
                composition.actor != actor
                or composition.artifacts is not artifacts
                or composition.audit is not audit
            ):
                raise ValueError("rule_runner_composition_ports_disagree")
        if isinstance(artifacts, GovernedArtifactStore):
            require_artifact_scope(artifacts, actor)
        self.runtime = runtime
        self.actor = actor
        self.artifacts = artifacts
        self.audit = audit
        self.policy = policy or PolicyEngine()
        self.composition = composition

    @property
    def production_mode(self) -> bool:
        return self.composition is not None and self.composition.mode == "product"

    def _gateway(self) -> ToolGateway:
        return ToolGateway(
            self.policy,
            self.audit,
            production_mode=self.production_mode,
            budgets=self.composition.budgets if self.composition is not None else None,
        )

    def invoke(self, request: ToolRequest, *, trace_id: str):
        if (request.user_id, request.tenant_id, request.project_id) != (
            self.actor.user_id,
            self.actor.tenant_id,
            self.actor.project_id,
        ) or not set(request.data_domains) <= set(self.actor.data_domains):
            raise PermissionError("rule_runner_actor_scope_mismatch")
        if request.tool_name != "rules.execute" or request.resource != "rule-workflow":
            raise ValueError("rule_runner_request_target_mismatch")
        if self.composition is not None:
            # A composed task and trace belong to the operator, not the caller:
            # evidence must land in the composed task and both audit records and
            # artifacts must join on the composed trace.
            identity = self.composition.identity
            identity.resolve_task(request.task_id, code="rule_runner_task_scope_mismatch")
            trace_id = identity.resolve_trace(trace_id)
        gateway = self._gateway()

        def persist(arguments):
            ref = self.artifacts.put_json(
                task_id=request.task_id,
                artifact_type="rule.workflow.evidence",
                value=arguments,
                created_by="rule-workflow",
            )
            # Verify the stored bytes before advertising the reference.
            self.artifacts.read_bytes(ref)
            return ref.model_dump(mode="json")

        def execute(arguments):
            from generaldataagent.contracts.rule_tools import RuleAnalysisRequest

            proposal = RuleAnalysisRequest.model_validate(arguments)
            thread = str(request.task_id)
            if request.action == "run":
                thread, state = self.runtime.run(proposal, self.actor, thread_id=thread)
            elif request.action == "resume":
                state = self.runtime.resume(thread, proposal, self.actor)
            else:
                raise ValueError("unsupported_rule_runner_action")
            evidence = {
                "task_id": str(request.task_id),
                "thread_id": thread,
                "request": proposal.model_dump(mode="json"),
                "source_hashes": state["source_hashes"],
                "binding_hash": state["binding_hash"],
                "status": state["status"],
                "verification": state.get("verification"),
                "computation": state.get("computation"),
                "delivery": state.get("delivery"),
            }
            artifact_request = ToolRequest(
                task_id=UUID(thread),
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                agent_role=request.agent_role,
                tool_name="artifact.write",
                action="create",
                resource="rule-workflow-evidence",
                arguments=evidence,
                data_domains=request.data_domains,
            )
            ref = gateway.invoke(artifact_request, trace_id=trace_id)
            return {
                "status": state["status"],
                "thread_id": thread,
                "evidence": ref,
                "production_accepted": False,
            }

        gateway.register("rules.execute", execute)
        gateway.register("artifact.write", persist)
        return gateway.invoke(request, trace_id=trace_id)
