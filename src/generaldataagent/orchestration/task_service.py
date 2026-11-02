"""Authenticated local task acceptance and one-task-at-a-time durable worker port."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from generaldataagent.contracts import ActorContext, Budget, MetricQuery
from generaldataagent.orchestration.checkpoints import ProtectedSqliteSaver
from generaldataagent.orchestration.metric_analysis import MetricAnalysisRuntime
from generaldataagent.orchestration.task_events import TaskEventJournal
from generaldataagent.orchestration.task_queue import (
    MetricTaskClaim,
    MetricTaskQueue,
    MetricTaskRecord,
    TaskLeaseLost,
    canonical_actor,
)
from generaldataagent.policy.budgets import BudgetExceededError, TaskBudgetLedger
from generaldataagent.security.identity import SessionBroker


@dataclass(frozen=True)
class MetricTaskExecution:
    claim: MetricTaskClaim
    guard: Callable[[], None]


class MetricTaskService:
    """Local opt-in service. No web request starts a background thread for analysis.

    Operators explicitly call run_once from a supervised worker. Reopening the queue,
    session broker, budgets and checkpointer recovers accepted work after process restart.
    """

    def __init__(
        self,
        *,
        queue: MetricTaskQueue,
        broker: SessionBroker,
        events: TaskEventJournal,
        budgets: TaskBudgetLedger,
        runtime_factory: Callable[[MetricTaskExecution], MetricAnalysisRuntime],
    ):
        self.queue, self.broker, self.events = queue, broker, events
        self.budgets, self.runtime_factory = budgets, runtime_factory

    def _authorize_reference(self, reference: str, actor: ActorContext) -> None:
        identity = self.broker.authenticate_reference(reference)
        if (
            identity.business_constraint is not None
            or "analysis:run" not in identity.permissions
            or (identity.user_id, identity.tenant_id, identity.project_id)
            != (actor.user_id, actor.tenant_id, actor.project_id)
            or not set(actor.data_domains) <= identity.data_domains
        ):
            raise PermissionError("task_authorization_unavailable")

    def _reference(self, token: str, actor: ActorContext) -> str:
        reference = self.broker.session_reference(token)
        self._authorize_reference(reference, actor)
        return reference

    def submit(
        self, query: MetricQuery, actor: ActorContext, token: str, idempotency_key: str
    ) -> MetricTaskRecord:
        actor = canonical_actor(actor)
        reference = self._reference(token, actor)
        record = self.queue.submit(query, actor, reference, idempotency_key)
        # Two durable databases are intentionally not advertised as one transaction.
        # A retry or the worker repairs a missing event registration after acceptance.
        self.events.ensure_started(record.task_id, actor)
        return record

    def read(self, task_id: UUID, actor: ActorContext, token: str) -> MetricTaskRecord:
        self._reference(token, actor)
        return self.queue.read(task_id, actor)

    def retry(self, task_id: UUID, actor: ActorContext, token: str) -> MetricTaskRecord:
        reference = self._reference(token, actor)
        return self.queue.retry(task_id, actor, reference)

    def _runtime(self, execution: MetricTaskExecution) -> MetricAnalysisRuntime:
        runtime = self.runtime_factory(execution)
        claim = execution.claim
        if (
            not isinstance(runtime, MetricAnalysisRuntime)
            or runtime.production_mode
            or not isinstance(runtime.checkpointer, ProtectedSqliteSaver)
            or runtime.task_events is not self.events
            or runtime.budgets is not self.budgets
            or runtime.execution_guard is not execution.guard
            or runtime.composition is None
            or runtime.composition.actor != claim.actor
            or runtime.composition.task_id != claim.record.task_id
            or runtime.composition.trace_id != str(claim.record.task_id)
        ):
            raise ValueError("task_runtime_ports_disagree")
        return runtime

    def run_once(self) -> MetricTaskRecord | None:
        claim = self.queue.claim()
        if claim is None:
            return None
        status, failure = "interrupted", "execution_interrupted"
        try:
            with self.queue.execution(claim) as check_lease:

                def guard():
                    check_lease()
                    self._authorize_reference(claim.session_reference, claim.actor)

                guard()
                self.events.ensure_started(claim.record.task_id, claim.actor)
                # The ledger's existing idempotent registration never resets spent usage
                # or the deadline. Queue wait time does not start the execution budget.
                self.budgets.register(claim.record.task_id, claim.actor, Budget())
                self.budgets.ensure_active(claim.record.task_id, claim.actor)
                runtime = self._runtime(MetricTaskExecution(claim, guard))
                result = runtime.run(
                    claim.query,
                    claim.actor,
                    thread_id=str(claim.record.thread_id),
                    recover_registered=True,
                )
                guard()
                if result.get("task_id") != str(claim.record.task_id):
                    raise ValueError("task_runtime_result_mismatch")
                status, failure = result["status"], None
        except TaskLeaseLost:
            # An expired worker must not settle another worker's claim.
            raise
        except BudgetExceededError:
            status, failure = "failed", "budget_exhausted"
        except PermissionError:
            status, failure = "interrupted", "authorization_unavailable"
        except ValueError:
            status, failure = "failed", "invalid_task"
        except Exception:
            status, failure = "interrupted", "execution_interrupted"
        return self.queue.settle(claim, status, failure)
