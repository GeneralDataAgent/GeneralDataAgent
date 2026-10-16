"""Human-reviewed model-registration drafts with no deployment capability."""

from datetime import UTC, datetime, timedelta

from generaldataagent.contracts import (
    AgentRole,
    ApprovalRequest,
    ArtifactClassification,
    ArtifactRef,
    ModelCard,
    ModelRegistrationDraft,
    RiskLevel,
    ToolRequest,
)
from generaldataagent.policy import ApprovalStore, PolicyEngine


class ModelRegistrationWorkflow:
    def __init__(self, *, approvals: ApprovalStore, policy: PolicyEngine | None = None) -> None:
        self.approvals = approvals
        self.policy = policy or PolicyEngine()

    def request_review(
        self,
        *,
        task_id,
        model_card: ModelCard,
        model_artifact: ArtifactRef,
        user_id: str,
        tenant_id: str,
        project_id: str,
        data_domain: str,
        expires_at: datetime | None = None,
    ) -> tuple[ModelRegistrationDraft, ApprovalRequest]:
        draft = ModelRegistrationDraft(
            task_id=task_id,
            model_card=model_card,
            model_artifact=model_artifact,
        )
        request = self._tool_request(
            draft=draft,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            data_domain=data_domain,
        )
        decision = self.policy.evaluate(request)
        if decision.decision != "require_approval":
            raise PermissionError("model registration drafts must require approval")
        approval = self.approvals.create_request(
            request,
            impact_scope="Create a registry draft only; model deployment remains prohibited.",
            cost_estimate="No serving infrastructure or production traffic.",
            classification=ArtifactClassification.INTERNAL,
            rollback_plan="Discard the unregistered draft; no production state is changed.",
            expires_at=expires_at or datetime.now(UTC) + timedelta(hours=24),
        )
        return draft, approval

    def finalize_approved_draft(
        self,
        *,
        draft: ModelRegistrationDraft,
        approval_id,
        user_id: str,
        tenant_id: str,
        project_id: str,
        data_domain: str,
    ) -> ModelRegistrationDraft:
        if draft.status != "pending_review":
            raise ValueError("only pending model-registration drafts can be finalized")
        request = self._tool_request(
            draft=draft,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            data_domain=data_domain,
        )
        self.approvals.consume(approval_id, request)
        return ModelRegistrationDraft.model_validate(
            draft.model_dump(mode="python")
            | {"status": "approved_draft", "approval_id": approval_id}
        )

    @staticmethod
    def _tool_request(
        *,
        draft: ModelRegistrationDraft,
        user_id: str,
        tenant_id: str,
        project_id: str,
        data_domain: str,
    ) -> ToolRequest:
        return ToolRequest(
            task_id=draft.task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_role=AgentRole.DATA_SCIENTIST,
            tool_name="model.registry",
            action="submit_draft",
            resource=f"{draft.model_card.model_name}:{draft.model_card.model_version}",
            arguments={
                "draft_id": str(draft.draft_id),
                "experiment_id": str(draft.model_card.experiment_id),
                "model_artifact_hash": draft.model_artifact.content_hash,
                "deployment_permitted": False,
            },
            data_domains=(data_domain,),
            requested_risk=RiskLevel.R2,
        )
