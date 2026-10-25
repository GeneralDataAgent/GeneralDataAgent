"""Deliver verified rule results as an evidence-backed product package.

The workflow already refuses to deliver when verification fails. This module is
the product-side counterpart: it turns a completed, verified thread into the
same ``DeliveryPackage`` the certified-metric path produces, so the Reporter and
API consume one shape rather than two.

Three invariants are enforced here rather than left to callers:

* Only a ``completed`` thread whose verification passed can be packaged.
* Every claim cites an artifact created by this same task.
* The task is registered with the feedback registry before the package is
  returned, so feedback can only ever attach to a package that existed.

Packaging runs through ``ToolGateway`` under the ``rules.deliver`` rule, so a
delivery is authorized and audited exactly like rule execution. Each delivery
owns its gateway and registers one ``artifact.write`` handler. Every package
artifact is separately authorized and audited before storage, using the caller's
role and scope.

This is a development adapter. It does not perform release admission, governed
retention or production-mode checks, and it never reports production acceptance.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import FeedbackRegistry
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    ChartSpec,
    DeliveryPackage,
    GroundedClaim,
    TableSpec,
    ToolRequest,
)
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime
from generaldataagent.policy import PolicyEngine
from generaldataagent.tools.gateway import ToolGateway

_ANSWER_COLUMN = "answer"
# ``GroundedClaim.text`` is bounded at 2000 characters by the delivery contract.
_CLAIM_LIMIT = 2_000


class RuleDeliveryError(ValueError):
    """Stable refusal code; never echoes the underlying result content."""

    CODES = frozenset(
        {
            "rule_delivery_requires_verified_thread",
            "rule_delivery_requires_completed_thread",
            "rule_delivery_unknown_operation",
            "rule_delivery_evidence_missing",
            "rule_delivery_already_packaged",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unknown rule delivery failure code")
        self.code = code
        super().__init__(code)


def _item_count(delivery: dict, operation: str) -> int | None:
    """The stored answer length, counted from the source list, not from text.

    ``None`` for scalar operations, which never overflow the claim contract.
    """
    if operation not in {"select", "applicable"}:
        return None
    items = delivery.get("items")
    return len(items) if isinstance(items, list) else None


def answer_rows(delivery: dict, operation: str) -> list[tuple[object, ...]]:
    """One deterministic row per answer value; order follows the operation.

    Also used by the Supervisor path to show a specialist the computed answer,
    so there is one definition of what an answer looks like.
    """
    if operation == "applicable":
        items = delivery.get("items")
        if not isinstance(items, list):
            raise RuleDeliveryError("rule_delivery_evidence_missing")
        return [("applicable_rules", ", ".join(str(item) for item in items) if items else "[]")]
    if operation == "counterfactual":
        delta = delivery.get("delta")
        if delta is None:
            raise RuleDeliveryError("rule_delivery_evidence_missing")
        return [("delta", delta)]
    if operation == "average_fee":
        average = delivery.get("average_fee")
        if average is None:
            raise RuleDeliveryError("rule_delivery_evidence_missing")
        return [("average_fee", average)]
    if operation == "select":
        items = delivery.get("items")
        if not isinstance(items, list):
            raise RuleDeliveryError("rule_delivery_evidence_missing")
        return [("selected_rules", ", ".join(str(item) for item in items))]
    raise RuleDeliveryError("rule_delivery_unknown_operation")


class RuleDeliveryRuntime:
    """Package a verified rule thread as a delivery with cited artifacts."""

    def __init__(
        self,
        *,
        workflow: RuleWorkflowRuntime,
        artifacts: LocalArtifactStore | GovernedArtifactStore,
        feedback: FeedbackRegistry | None = None,
        audit: JsonlAuditSink | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        if not isinstance(artifacts, LocalArtifactStore | GovernedArtifactStore):
            raise ValueError("rule_delivery_requires_a_supported_artifact_store")
        self.workflow = workflow
        self.artifacts = artifacts
        self.feedback = feedback
        self.audit = audit
        self.policy = policy or PolicyEngine()

    def deliver(
        self,
        thread_id: str,
        request: RuleAnalysisRequest,
        actor: ActorContext,
        *,
        task_id: UUID,
        agent_role: AgentRole | str = AgentRole.DATA_ANALYST,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if (actor.user_id, actor.tenant_id, actor.project_id) == ("", "", ""):
            raise PermissionError("rule_delivery_actor_required")
        if isinstance(self.artifacts, GovernedArtifactStore):
            bound = self.artifacts.actor
            if (bound.tenant_id, bound.project_id) != (
                actor.tenant_id,
                actor.project_id,
            ) or not set(actor.data_domains) <= set(bound.data_domains):
                raise PermissionError("artifact_store_actor_scope_mismatch")
        # A gateway per delivery, exactly as ``GovernedRuleRunner`` does: handlers
        # close over this call's actor and task so a shared gateway can never be
        # repointed at another tenant.
        gateway = ToolGateway(self.policy, self.audit)
        trace = trace_id or f"rule-delivery-{task_id}"

        def persist(arguments):
            ref = self.artifacts.put_json(task_id=task_id, **arguments)
            self.artifacts.read_bytes(ref)
            return ref

        def write_artifact(**arguments):
            artifact_request = ToolRequest(
                task_id=task_id,
                user_id=actor.user_id,
                tenant_id=actor.tenant_id,
                project_id=actor.project_id,
                agent_role=agent_role,
                tool_name="artifact.write",
                action="create",
                resource=arguments["artifact_type"],
                arguments=arguments,
                data_domains=actor.data_domains,
            )
            return gateway.invoke(artifact_request, trace_id=trace)

        gateway.register("artifact.write", persist)
        gateway.register(
            "rules.deliver",
            lambda arguments: self._package(
                arguments, actor=actor, task_id=task_id, write_artifact=write_artifact
            ),
        )
        tool_request = ToolRequest(
            task_id=task_id,
            user_id=actor.user_id,
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            agent_role=agent_role,
            tool_name="rules.deliver",
            action="deliver",
            resource="rule-workflow",
            arguments={
                "thread_id": thread_id,
                "request": request.model_dump(mode="json"),
            },
            data_domains=actor.data_domains,
        )
        return gateway.invoke(tool_request, trace_id=trace)

    def _package(
        self, arguments: dict[str, Any], *, actor: ActorContext, task_id: UUID, write_artifact
    ) -> dict[str, Any]:
        """Authorized handler: build and verify the package for one thread."""
        thread_id = arguments["thread_id"]
        request = RuleAnalysisRequest.model_validate(arguments["request"])
        # Read-only: packaging must never finish an interrupted task.
        state = self.workflow.state_of(thread_id, request, actor)
        if state.get("status") != "completed":
            raise RuleDeliveryError("rule_delivery_requires_completed_thread")
        verification = state.get("verification") or {}
        if verification.get("passed") is not True or verification.get("failures"):
            raise RuleDeliveryError("rule_delivery_requires_verified_thread")
        delivery = state.get("delivery") or {}
        operation = request.operation
        rows = answer_rows(delivery, operation)
        if self.feedback is not None and self.feedback.registered(task_id, actor):
            raise RuleDeliveryError("rule_delivery_already_packaged")

        data = write_artifact(
            artifact_type="rule_analysis_result",
            value={
                "operation": operation,
                "request": request.model_dump(mode="json"),
                "answer": rows,
                "delivery": delivery,
                "verification": verification,
                "source_hashes": state.get("source_hashes", {}),
                "thread_id": thread_id,
            },
            created_by="data-analyst",
        )
        table = TableSpec(
            columns=(_ANSWER_COLUMN, "value"),
            rows=tuple(rows),
            data_artifact_uri=data.uri,
        )
        chart = ChartSpec(
            chart_type="kpi",
            title=f"rule analysis: {operation}",
            data_artifact_uri=data.uri,
        )
        chart_artifact = write_artifact(
            artifact_type="chart_spec",
            value=chart.model_dump(mode="json"),
            created_by="deterministic-reporter",
            lineage=(data.artifact_id,),
        )
        claim = GroundedClaim(
            claim_id="rule-result",
            claim_type="fact",
            text=self._claim_text(operation, rows, item_count=_item_count(delivery, operation)),
            evidence_uris=(data.uri,),
        )
        claims_artifact = write_artifact(
            artifact_type="rule_claims",
            value=[claim.model_dump(mode="json")],
            created_by="verifier",
            lineage=(data.artifact_id,),
        )
        # Read every reference back before advertising it.
        for ref in (data, chart_artifact, claims_artifact):
            self.artifacts.read_bytes(ref)

        package = DeliveryPackage(
            metric=f"rule:{operation}",
            columns=table.columns,
            rows=table.rows,
            row_count=len(table.rows),
            table=table,
            chart=chart,
            summary=claim.text,
            insights=(),
            limitations=self._limitations(state),
            claims=(claim,),
            narrative_model=None,
            evidence=(data, chart_artifact, claims_artifact),
        )
        if self.feedback is not None:
            self.feedback.register_task(
                task_id, actor, content_hash(package.model_dump(mode="json"))
            )
        return {
            "delivery": package.model_dump(mode="json"),
            "thread_id": thread_id,
            "task_id": str(task_id),
            "verification": verification,
            "source_hashes": state.get("source_hashes", {}),
            "production_accepted": False,
        }

    @staticmethod
    def _claim_text(operation: str, rows, *, item_count: int | None = None) -> str:
        label, value = rows[0]
        text = f"Rule analysis ({operation}) produced {label} = {value}."
        if len(text) <= _CLAIM_LIMIT:
            return text
        # A long ID list overflows the claim contract (2000 characters). Never
        # truncate it: a shortened list would read as the complete answer. The
        # full list is in the cited artifact, so the claim states its size.
        if item_count is None:
            raise RuleDeliveryError("rule_delivery_evidence_missing")
        return (
            f"Rule analysis ({operation}) produced {label} with {item_count} values; "
            "the complete list is in the cited artifact."
        )

    @staticmethod
    def _limitations(state: dict) -> tuple[str, ...]:
        limitations = [
            "Deterministic rule execution over the supplied annex and documents.",
            "Verification re-derives the result from the same code path; it is not an "
            "independent semantic oracle.",
        ]
        if state.get("monthly_context", {}).get("scope") == "merchant_and_transactions":
            limitations.append(
                "Merchant/month thresholds use the whole natural month even when the "
                "question scopes a single day."
            )
        return tuple(limitations)


__all__ = ["RuleDeliveryError", "RuleDeliveryRuntime"]
