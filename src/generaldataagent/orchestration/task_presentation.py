"""Read-side consumer of confirmed display preferences; never changes analysis inputs."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts import ActorContext, ArtifactRef
from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import (
    ConfirmDisplayPreference,
    ConfirmedDisplayPreferenceStore,
    DisplayPreferenceRecord,
    DisplayPreferenceState,
    MetricDisplayPreference,
    PreferenceConflict,
    canonical_hash,
)
from generaldataagent.orchestration.task_service import MetricTaskService


class MetricTaskPresentation(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    thread_id: UUID
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    preference: DisplayPreferenceState
    layout: Literal["summary", "table"]
    summary: str = Field(min_length=1, max_length=2000)
    row_count: int = Field(ge=0)
    columns: tuple[str, ...] = Field(min_length=1)
    rows: tuple[tuple[object, ...], ...]
    has_more_rows: bool
    evidence: tuple[ArtifactRef, ...] = Field(min_length=1)
    presentation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def presentation_binding(self):
        value = (
            self.preference.record.value if self.preference.record else MetricDisplayPreference()
        )
        if (
            self.layout != value.layout
            or len(self.rows) > value.preview_rows
            or len(self.rows) > min(self.row_count, 50)
            or any(len(row) != len(self.columns) for row in self.rows)
            or self.has_more_rows != (len(self.rows) < self.row_count)
            or (self.layout == "summary" and self.rows)
            or self.presentation_hash
            != canonical_hash(self.model_dump(mode="json", exclude={"presentation_hash"}))
        ):
            raise ValueError("metric_presentation_binding_invalid")
        return self


class MetricDisplayPreferenceService:
    """Local, opt-in application service; raw/candidate memory is not accepted.

    Only post-analysis presentation uses the preference. Original queue payloads,
    checkpoint recovery, MetricTaskResult v1 and certified calculations stay unchanged.
    Each presentation embeds its private immutable preference revision and result hash.
    """

    def __init__(self, *, tasks: MetricTaskService, store: ConfirmedDisplayPreferenceStore):
        self.tasks, self.store = tasks, store

    def _guard(self, actor: ActorContext, token: str):
        reference = self.tasks._reference(token, actor)
        return lambda: self.tasks._authorize_reference(reference, actor)

    def current(self, actor: ActorContext, token: str) -> DisplayPreferenceState:
        guard = self._guard(actor, token)
        state = self.store.current(actor)
        guard()
        return state

    def history(self, actor: ActorContext, token: str, version: int) -> DisplayPreferenceRecord:
        guard = self._guard(actor, token)
        record = self.store.history(actor, version)
        guard()
        return record

    def confirm(self, actor: ActorContext, token: str, request: ConfirmDisplayPreference):
        return self.store.confirm(actor, request, guard=self._guard(actor, token))

    def revoke(self, actor: ActorContext, token: str, expected_version: int):
        return self.store.revoke(actor, expected_version, guard=self._guard(actor, token))

    def presentation(
        self, task_id: UUID, actor: ActorContext, token: str
    ) -> MetricTaskPresentation:
        guard = self._guard(actor, token)
        context = self.tasks.queue.result_context(task_id, actor)
        # Exact original task domain-view, not the caller's possibly broader headers.
        state = self.store.current(context.actor)
        result = self.tasks.result(task_id, actor, token)
        guard()
        value = state.record.value if state.record is not None else MetricDisplayPreference()
        delivery = result.delivery
        rows = delivery.rows[: value.preview_rows] if value.layout == "table" else ()
        fields = dict(
            schema_version="1.0",
            task_id=str(task_id),
            thread_id=str(result.thread_id),
            result_hash=canonical_hash(result.model_dump(mode="json")),
            preference=state.model_dump(mode="json"),
            layout=value.layout,
            summary=delivery.summary,
            row_count=delivery.row_count,
            columns=list(delivery.columns),
            rows=[list(row) for row in rows],
            has_more_rows=len(rows) < delivery.row_count,
            evidence=[ref.model_dump(mode="json") for ref in delivery.evidence],
        )
        if state.record is not None:
            self.store.emit(state.record, "display_preference.presentation_intent", task_id=task_id)
        if self.store.current(context.actor) != state:
            raise PreferenceConflict("display_preference_changed_during_read")
        guard()
        return MetricTaskPresentation(**fields, presentation_hash=canonical_hash(fields))
