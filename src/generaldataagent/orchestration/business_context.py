"""Application service for explicit, private business definitions and project background."""

from generaldataagent.contracts import ActorContext
from generaldataagent.memory.business_context import (
    BusinessContextRecord,
    BusinessContextState,
    ConfirmBusinessContext,
    ConfirmedBusinessContextStore,
)
from generaldataagent.orchestration.task_service import MetricTaskService


class BusinessContextService:
    """Local, opt-in service; raw candidates and model output are never accepted here.

    Stored background is citable context only. It does not change certified metric
    calculations, tool policy, approvals, budgets or task payloads; execution-time
    injection remains a separate governed slice.
    """

    def __init__(self, *, tasks: MetricTaskService, store: ConfirmedBusinessContextStore):
        self.tasks, self.store = tasks, store

    def _guard(self, actor: ActorContext, token: str):
        reference = self.tasks._reference(token, actor)
        return lambda: self.tasks._authorize_reference(reference, actor)

    def current(self, actor: ActorContext, token: str) -> BusinessContextState:
        guard = self._guard(actor, token)
        state = self.store.current(actor)
        guard()
        return state

    def history(self, actor: ActorContext, token: str, version: int) -> BusinessContextRecord:
        guard = self._guard(actor, token)
        record = self.store.history(actor, version)
        guard()
        return record

    def confirm(self, actor: ActorContext, token: str, request: ConfirmBusinessContext):
        return self.store.confirm(actor, request, guard=self._guard(actor, token))

    def revoke(self, actor: ActorContext, token: str, expected_version: int):
        return self.store.revoke(actor, expected_version, guard=self._guard(actor, token))
