"""What a service identity may run: one task, on one immutable snapshot.

A scheduled workload authenticates the service, not a person. Its authority to
act for a schedule's owner comes from the operator's server-side delegation, and
its reach must stop at the snapshot the schedule was declared for: an experiment
that names another task or another snapshot is not a smaller request, it is a
different one. The scope is declarative and trusted runtime state — never read
from a token, a header or a request body.
"""

from dataclasses import dataclass
from uuid import UUID

from generaldataagent.contracts import ActorContext


@dataclass(frozen=True, slots=True)
class ScienceWorkloadScope:
    """The exact actor, task and snapshot a workload may run experiments on."""

    user_id: str
    tenant_id: str
    project_id: str
    data_domains: frozenset[str]
    task_id: UUID
    snapshot_id: UUID


def require_science_scope(
    scope: ScienceWorkloadScope,
    *,
    actor: ActorContext,
    task_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Refuse an experiment outside the declared reach; name why it was refused."""
    if (actor.user_id, actor.tenant_id, actor.project_id) != (
        scope.user_id,
        scope.tenant_id,
        scope.project_id,
    ) or not set(actor.data_domains) <= set(scope.data_domains):
        raise PermissionError("science_workload_actor_mismatch")
    if task_id != scope.task_id:
        raise PermissionError("science_workload_task_mismatch")
    if snapshot_id != scope.snapshot_id:
        raise PermissionError("science_workload_snapshot_mismatch")
