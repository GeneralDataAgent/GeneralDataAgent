"""Bounded, reauthorized application SSE; no raw graph/model protocol is forwarded."""

import asyncio
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.orchestration.task_queue import TaskQueueConflict
from generaldataagent.orchestration.task_service import MetricTaskService


class TaskStreamBusy(RuntimeError):
    pass


class TaskStreamEnd(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["terminal", "duration", "frame_limit", "unavailable"]
    after: int = Field(ge=0, strict=True)


@dataclass(frozen=True)
class TaskStreamLimits:
    max_seconds: float = 30
    poll_seconds: float = 0.5
    keepalive_seconds: float = 10
    max_frames: int = 200
    max_connections: int = 8
    max_per_principal: int = 2

    def __post_init__(self):
        for value, low, high in (
            (self.max_seconds, 0.1, 60),
            (self.poll_seconds, 0.01, 5),
            (self.keepalive_seconds, 0.1, 30),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError("invalid_task_stream_limits")
        for value, low, high in (
            (self.max_frames, 2, 1000),
            (self.max_connections, 1, 100),
            (self.max_per_principal, 1, self.max_connections),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid_task_stream_limits")


def _frame(kind: str, value, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return prefix + f"event: {kind}\ndata: {json.dumps(value, separators=(',', ':'))}\n\n"


class MetricTaskStreams:
    """Per-process pilot admission. Reverse proxies still own transport connection limits."""

    def __init__(
        self,
        service: MetricTaskService,
        *,
        limits: TaskStreamLimits | None = None,
        enabled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        self.service, self.limits, self.enabled, self.clock = (
            service,
            limits or TaskStreamLimits(),
            enabled,
            clock,
        )
        self._lock = Lock()
        self._connections: dict[str, tuple[tuple[str, str, str], float]] = {}

    def _guard(self, task_id: UUID, actor: ActorContext, reference: str):
        if self.enabled is not None:
            try:
                if self.enabled() is not True:
                    raise PermissionError("task_stream_unavailable")
            except Exception as exc:
                raise PermissionError("task_stream_unavailable") from exc
        self.service._authorize_reference(reference, actor)
        return self.service.queue.read(task_id, actor)

    def _snapshot(self, prepared, after: int):
        record = self._guard(prepared.task_id, prepared.actor, prepared.reference)
        page = self.service.events.read(prepared.task_id, prepared.actor, after=after, limit=50)
        self._guard(prepared.task_id, prepared.actor, prepared.reference)
        return record, page

    def _acquire(self, actor: ActorContext) -> tuple[str, float]:
        principal = (actor.user_id, actor.tenant_id, actor.project_id)
        now = self.clock()
        with self._lock:
            self._connections = {
                key: value for key, value in self._connections.items() if value[1] > now
            }
            if (
                len(self._connections) >= self.limits.max_connections
                or sum(value[0] == principal for value in self._connections.values())
                >= self.limits.max_per_principal
            ):
                raise TaskStreamBusy("task_stream_capacity_exhausted")
            slot, deadline = str(uuid4()), now + self.limits.max_seconds
            # Reap abandoned response setup; no background polling thread is created.
            self._connections[slot] = (principal, deadline + 5)
            return slot, deadline

    def _release(self, slot: str):
        with self._lock:
            self._connections.pop(slot, None)

    def prepare(
        self,
        task_id: UUID,
        actor: ActorContext,
        token: str,
        *,
        after: int | None = None,
        last_event_id: str | None = None,
    ) -> "PreparedTaskStream":
        reference = self.service._reference(token, actor)
        self._guard(task_id, actor, reference)
        if last_event_id is not None:
            try:
                if not isinstance(last_event_id, str) or len(last_event_id) > 80:
                    raise ValueError("event_id")
                prefix, value = last_event_id.split(":")
                if (
                    prefix != str(task_id)
                    or not value.isascii()
                    or not value.isdecimal()
                    or len(value) > 10
                ):
                    raise ValueError("event_id")
                sequence = int(value)
                if after is not None and after != sequence:
                    raise ValueError("conflicting_cursor")
                after = sequence
            except (ValueError, TypeError) as exc:
                raise TaskQueueConflict("task_stream_cursor_invalid") from exc
        cursor = 0 if after is None else after
        try:
            self.service.events.read(task_id, actor, after=cursor, limit=1)
        except ValueError as exc:
            raise TaskQueueConflict("task_stream_cursor_invalid") from exc
        slot, deadline = self._acquire(actor)
        return PreparedTaskStream(self, task_id, actor, reference, cursor, slot, deadline)


@dataclass(frozen=True)
class PreparedTaskStream:
    controller: MetricTaskStreams
    task_id: UUID
    actor: ActorContext
    reference: str
    after: int
    slot: str
    deadline: float

    def close(self):
        self.controller._release(self.slot)

    async def iterate(self, disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
        control = self.controller
        after, count, previous = self.after, 0, None
        last_sent = control.clock()
        reason = "duration"

        async def authorized():
            if await disconnected():
                return False
            await asyncio.to_thread(control._guard, self.task_id, self.actor, self.reference)
            return True

        try:
            while control.clock() < self.deadline:
                if not await authorized():
                    return
                record, page = await asyncio.to_thread(control._snapshot, self, after)
                if record != previous:
                    if not await authorized():
                        return
                    if control.clock() >= self.deadline:
                        break
                    yield _frame("task", record.model_dump(mode="json"))
                    previous, count, last_sent = record, count + 1, control.clock()
                for event in page.events:
                    if count >= control.limits.max_frames:
                        reason = "frame_limit"
                        break
                    if control.clock() >= self.deadline:
                        break
                    if not await authorized():
                        return
                    after = event.sequence
                    yield _frame(
                        "progress", event.model_dump(mode="json"), f"{self.task_id}:{after}"
                    )
                    count, last_sent = count + 1, control.clock()
                if count >= control.limits.max_frames:
                    reason = "frame_limit"
                    break
                if record.status in {"completed", "failed", "needs_human"} and not page.has_more:
                    # Recheck the tail after a queue/journal interleaving, before declaring drained.
                    _, tail = await asyncio.to_thread(control._snapshot, self, after)
                    if tail.events:
                        continue
                    reason = "terminal"
                    break
                if page.has_more:
                    continue
                if control.clock() - last_sent >= control.limits.keepalive_seconds:
                    if not await authorized():
                        return
                    yield ": keep-alive\n\n"
                    last_sent = control.clock()
                await asyncio.sleep(
                    min(control.limits.poll_seconds, max(0, self.deadline - control.clock()))
                )
            if await authorized():
                yield _frame(
                    "stream_end", TaskStreamEnd(reason=reason, after=after).model_dump(mode="json")
                )
        except Exception:
            if not await disconnected():
                yield _frame(
                    "stream_end",
                    TaskStreamEnd(reason="unavailable", after=after).model_dump(mode="json"),
                )
        finally:
            self.close()
