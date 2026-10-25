"""Explicit execution compositions for a governed task.

One task needs the same things no matter which capability runs it: who is
acting, which task and trace the work belongs to, where evidence lands, and who
verifies the record of what happened. Research and product executions need that
same shape but different guarantees, so the difference is declared here, once,
instead of being re-derived (and re-forgotten) at every entry point.

``research`` admits local development ports: unverified JSONL audit and an
unscoped content-addressed artifact store. ``product`` requires the ports that
make a refusal possible — verified audit, a governed artifact store bound to
the same actor scope, and a task budget ledger. Both are constructor checks,
so a product composition cannot be built from development ports by passing a
flag.

The composition is a *statement about wiring*, not an authorization. Selecting
``product`` does not admit an actor, a release, or a data source; those stay
with their existing gates. Nothing here reports acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore, require_artifact_scope
from generaldataagent.contracts import ActorContext
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.policy.budgets import TaskBudgetLedger

CompositionMode = Literal["research", "product"]

_AUDIT_SINKS = (JsonlAuditSink, VerifiedAuditSink)
_ARTIFACT_STORES = (LocalArtifactStore, GovernedArtifactStore)


class CompositionError(ValueError):
    """Stable refusal code for an incoherent execution composition."""

    CODES = frozenset(
        {
            "composition_mode_unknown",
            "composition_actor_required",
            "composition_artifact_store_required",
            "composition_audit_required",
            "composition_product_requires_governed_artifacts",
            "composition_product_requires_verified_audit",
            "composition_product_requires_budgets",
            "composition_budgets_invalid",
            "composition_artifact_scope_mismatch",
            "composition_task_id_invalid",
            "composition_trace_id_invalid",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unknown composition failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExecutionComposition:
    """The ports and identity one task executes through, plus its guarantees.

    ``actor`` is the single identity every port must agree on. A governed
    artifact store is constructed for one actor scope, so binding it here
    refuses the mismatch at composition time rather than after a write.

    ``task_id`` and ``trace_id`` are optional so a caller that owns neither can
    still declare its ports. When they are present, a capability must use them
    rather than mint its own: evidence then belongs to the task the operator
    composed, and both audit records and artifacts join on one trace.
    """

    mode: CompositionMode
    actor: ActorContext
    artifacts: LocalArtifactStore
    audit: JsonlAuditSink | VerifiedAuditSink
    budgets: TaskBudgetLedger | None = None
    task_id: UUID | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("research", "product"):
            raise CompositionError("composition_mode_unknown")
        if not isinstance(self.actor, ActorContext):
            raise CompositionError("composition_actor_required")
        if not isinstance(self.artifacts, _ARTIFACT_STORES):
            raise CompositionError("composition_artifact_store_required")
        if not isinstance(self.audit, _AUDIT_SINKS):
            raise CompositionError("composition_audit_required")
        if self.task_id is not None and not isinstance(self.task_id, UUID):
            raise CompositionError("composition_task_id_invalid")
        if self.trace_id is not None and (
            not isinstance(self.trace_id, str) or not self.trace_id.strip()
        ):
            raise CompositionError("composition_trace_id_invalid")
        if isinstance(self.artifacts, GovernedArtifactStore):
            try:
                require_artifact_scope(self.artifacts, self.actor)
            except PermissionError:
                raise CompositionError("composition_artifact_scope_mismatch") from None
        if self.mode == "product":
            if not isinstance(self.artifacts, GovernedArtifactStore):
                raise CompositionError("composition_product_requires_governed_artifacts")
            if not isinstance(self.audit, VerifiedAuditSink):
                raise CompositionError("composition_product_requires_verified_audit")
            if not isinstance(self.budgets, TaskBudgetLedger):
                raise CompositionError("composition_product_requires_budgets")
        elif self.budgets is not None and not isinstance(self.budgets, TaskBudgetLedger):
            raise CompositionError("composition_budgets_invalid")

    @property
    def production_ready(self) -> bool:
        """Wiring completeness only; not an acceptance or admission claim."""
        return self.mode == "product"


__all__ = ["CompositionError", "CompositionMode", "ExecutionComposition"]
