"""Bound concurrent child execution in one parent budget session."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from generaldataagent.contracts.child_budget import (
    BudgetReservationAmount,
    BudgetUsageAmount,
    ChildBudgetReservation,
    ChildBudgetSettlement,
    ChildBudgetState,
)
from generaldataagent.contracts.child_execution import GovernorJob
from generaldataagent.routing.child_budgets import ChildBudgetBook, ChildBudgetRefused

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class ChildExecutionRefused(ValueError):
    """A fixed child stage was refused or could not converge."""


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """Committed result of a bounded child stage."""

    job_id: UUID
    stage_id: str
    status: Literal["completed"]
    results: dict[str, object]
    usage: dict[str, BudgetUsageAmount]
    states: dict[str, ChildBudgetState]


@dataclass(slots=True)
class _Invocation:
    child: str
    grant: ChildBudgetReservation
    timeout_at: float
    started_at: float
    done: threading.Event = field(default_factory=threading.Event)
    completed_at: float | None = None
    value: object | None = None
    error: Exception | None = None
    timed_out: bool = False


class ChildExecutionGovernor:
    """Run one fixed, read-only stage of at most two independent children."""

    def __init__(self, book: ChildBudgetBook) -> None:
        if not isinstance(book, ChildBudgetBook):
            raise ChildExecutionRefused("child_execution_book_invalid")
        self.book = book
        self.lock = threading.RLock()

    def run(self, job: GovernorJob) -> StageOutcome:
        """Reserve both children, execute them concurrently, and record usage."""
        self._validate_job(job)
        children = self._children(job)
        if job.cancellation is not None and job.cancellation.is_set():
            raise ChildExecutionRefused("child_execution_cancelled")

        with self.lock:
            started = time.monotonic()
            amounts = self._reservation_amounts(job, children)
            reservation_ids = {child: f"{job.stage_id}_{child}" for child in children}
            try:
                grants = self.book.reserve_many(
                    job.job_id,
                    {child: (reservation_ids[child], amounts[child]) for child in children},
                )
            except Exception as exc:  # noqa: BLE001
                raise ChildExecutionRefused("child_execution_reservation_failed") from exc

            invocations = {
                child: self._start(job, child, grants[child], started) for child in children
            }
            try:
                self._await(job, invocations, started)
            except ChildExecutionRefused:
                self._close(job, grants, invocations, "cancelled")
                raise

            results, usage, failed = self._collect(children, invocations)
            if failed:
                self._close(job, grants, invocations, "failed")
                raise ChildExecutionRefused("child_execution_failed")
            if job.cancellation is not None and job.cancellation.is_set():
                self._close(job, grants, invocations, "cancelled")
                raise ChildExecutionRefused("child_execution_cancelled")

            try:
                states = self.book.record_telemetry_many(
                    job.job_id,
                    {
                        child: (grants[child].reservation_digest, usage[child], 0)
                        for child in children
                    },
                )
            except ChildBudgetRefused as exc:
                self._close(job, grants, invocations, "failed")
                raise ChildExecutionRefused("child_execution_usage_refused") from exc

            return StageOutcome(
                job_id=job.job_id,
                stage_id=job.stage_id,
                status="completed",
                results=results,
                usage=usage,
                states=states,
            )

    def settle(
        self,
        job: GovernorJob,
        *,
        usage: dict[str, BudgetUsageAmount],
    ) -> dict[str, ChildBudgetSettlement]:
        """Settle every child of a completed stage in one transaction."""
        self._validate_job(job)
        children = self._children(job)
        if not isinstance(usage, dict) or set(usage) != set(children):
            raise ChildExecutionRefused("child_execution_settlement_invalid")
        items = {}
        for child in children:
            state = self.book.get(job.job_id, child)
            if state is None or state.status != "telemetry_recorded":
                raise ChildExecutionRefused("child_execution_settlement_invalid")
            value = usage[child]
            if not isinstance(value, BudgetUsageAmount):
                raise ChildExecutionRefused("child_execution_settlement_invalid")
            items[child] = (state.reservation_digest, value, state.retry_count)
        try:
            return self.book.settle_many(job.job_id, items)
        except ChildBudgetRefused as exc:
            raise ChildExecutionRefused("child_execution_settlement_refused") from exc

    @staticmethod
    def _validate_job(job: GovernorJob) -> None:
        if not isinstance(job, GovernorJob) or not _NAME.fullmatch(job.stage_id):
            raise ChildExecutionRefused("child_execution_job_invalid")

    @staticmethod
    def _child_name(child: object) -> str:
        if type(child) is not str or not _NAME.fullmatch(child):
            raise ChildExecutionRefused("child_execution_child_invalid")
        return child

    def _children(self, job: GovernorJob) -> tuple[str, ...]:
        children = tuple(self._child_name(child) for child in job.children)
        if len(set(children)) != len(children):
            raise ChildExecutionRefused("child_execution_child_invalid")
        return children

    def _reservation_amounts(
        self, job: GovernorJob, children: tuple[str, ...]
    ) -> dict[str, BudgetReservationAmount]:
        amounts = {}
        for child in children:
            if job.admission_fn is not None and job.admission_fn(child) is not True:
                raise ChildExecutionRefused("child_execution_admission_refused")
            amount = job.reserve_fn(child)
            if not isinstance(amount, BudgetReservationAmount):
                raise ChildExecutionRefused("child_execution_reservation_invalid")
            runtime = amount.runtime_milliseconds
            if runtime <= 0 or runtime > job.timeout_ms:
                raise ChildExecutionRefused("child_execution_reservation_invalid")
            amounts[child] = amount
        return amounts

    def _start(
        self,
        job: GovernorJob,
        child: str,
        grant: ChildBudgetReservation,
        started: float,
    ) -> _Invocation:
        runtime = grant.reservation.runtime_milliseconds or job.timeout_ms
        invocation = _Invocation(
            child=child,
            grant=grant,
            timeout_at=started + min(job.timeout_ms, runtime) / 1000,
            started_at=started,
        )

        def target() -> None:
            try:
                invocation.value = job.run_fn(job.job_id, child, grant)
            except Exception as exc:  # noqa: BLE001
                invocation.error = exc
            finally:
                invocation.completed_at = time.monotonic()
                invocation.done.set()

        threading.Thread(
            target=target,
            name=f"child-execution-{child}",
            daemon=True,
        ).start()
        return invocation

    @staticmethod
    def _await(
        job: GovernorJob,
        invocations: dict[str, _Invocation],
        started: float,
    ) -> None:
        stage_deadline = started + job.deadline_ms / 1000
        while True:
            if job.cancellation is not None and job.cancellation.is_set():
                raise ChildExecutionRefused("child_execution_cancelled")
            now = time.monotonic()
            if now >= stage_deadline:
                raise ChildExecutionRefused("child_execution_cancelled")
            pending = [
                invocation for invocation in invocations.values() if not invocation.done.is_set()
            ]
            for invocation in pending:
                if now >= invocation.timeout_at:
                    invocation.timed_out = True
            if any(
                invocation.done.is_set() and invocation.error is not None
                for invocation in invocations.values()
            ):
                return
            if any(invocation.timed_out for invocation in invocations.values()):
                return
            if not pending:
                return
            next_at = min(
                [stage_deadline]
                + [invocation.timeout_at for invocation in pending if not invocation.timed_out]
            )
            time.sleep(min(0.01, max(0.0, next_at - now)))

    def _collect(
        self,
        children: tuple[str, ...],
        invocations: dict[str, _Invocation],
    ) -> tuple[dict[str, object], dict[str, BudgetUsageAmount], bool]:
        results = {}
        usage = {}
        failed = False
        for child in children:
            invocation = invocations[child]
            timed_out = invocation.timed_out or (
                invocation.completed_at is not None
                and invocation.completed_at > invocation.timeout_at
            )
            if timed_out or invocation.error is not None or not invocation.done.is_set():
                failed = True
                usage[child] = BudgetUsageAmount(source="unknown")
                continue
            value = invocation.value
            measured = self._usage(invocation)
            if measured is None:
                failed = True
                usage[child] = BudgetUsageAmount(source="unknown")
                continue
            results[child] = value
            usage[child] = measured
        return results, usage, failed

    @staticmethod
    def _usage(invocation: _Invocation) -> BudgetUsageAmount | None:
        if invocation.completed_at is None or invocation.error is not None:
            return None
        if invocation.timed_out or invocation.completed_at > invocation.timeout_at:
            return None
        elapsed = int((invocation.completed_at - invocation.started_at) * 1000)
        if elapsed > (invocation.grant.reservation.runtime_milliseconds or 0):
            return None
        value = invocation.value
        usage = BudgetUsageAmount(
            source="tool_reported",
            tokens=getattr(value, "tokens", None),
            tool_calls=1,
            scan_bytes=getattr(value, "scan_bytes", None),
            cost_microunits=getattr(value, "cost_microunits", None),
            runtime_milliseconds=elapsed,
            complete=False,
        )
        reserved = invocation.grant.reservation
        if any(
            observed is not None and observed > bound
            for observed, bound in (
                (usage.tokens, reserved.tokens),
                (usage.tool_calls, reserved.tool_calls),
                (usage.scan_bytes, reserved.scan_bytes),
                (usage.cost_microunits, reserved.cost_microunits),
                (usage.runtime_milliseconds, reserved.runtime_milliseconds),
            )
        ):
            return None
        return usage

    def _close(
        self,
        job: GovernorJob,
        grants: dict[str, ChildBudgetReservation],
        invocations: dict[str, _Invocation],
        status: str,
    ) -> None:
        usages = {}
        for child in grants:
            measured = self._usage(invocations[child])
            usages[child] = measured or BudgetUsageAmount(source="unknown")
        try:
            self.book.close_many(
                job.job_id,
                status,
                {child: (grants[child].reservation_digest, usages[child]) for child in grants},
            )
        except ChildBudgetRefused as exc:
            raise ChildExecutionRefused("child_execution_close_failed") from exc
