"""Development-only policy/audit/artifact adapter for rule workflow execution."""

from uuid import UUID

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext, ToolRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy import PolicyEngine
from generaldataagent.tools.gateway import ToolGateway


class GovernedRuleRunner:
    """Route execution and evidence writes through existing gateway policy.

    Only development local stores are admitted. This adapter does not implement
    production release admission, source authorization or governed retention.
    """

    def __init__(
        self,
        *,
        runtime: RuleWorkflowRuntime,
        actor: ActorContext,
        artifacts: LocalArtifactStore,
        audit: JsonlAuditSink,
        policy: PolicyEngine | None = None,
    ):
        if type(artifacts) is not LocalArtifactStore or not isinstance(audit, JsonlAuditSink):
            raise ValueError("rule_runner_requires_development_artifacts_and_audit")
        self.runtime = runtime
        self.actor = actor
        self.artifacts = artifacts
        self.audit = audit
        self.policy = policy or PolicyEngine()

    def invoke(self, request: ToolRequest, *, trace_id: str):
        if (request.user_id, request.tenant_id, request.project_id) != (
            self.actor.user_id,
            self.actor.tenant_id,
            self.actor.project_id,
        ) or not set(request.data_domains) <= set(self.actor.data_domains):
            raise PermissionError("rule_runner_actor_scope_mismatch")
        if request.tool_name != "rules.execute" or request.resource != "rule-workflow":
            raise ValueError("rule_runner_request_target_mismatch")
        gateway = ToolGateway(self.policy, self.audit)

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
