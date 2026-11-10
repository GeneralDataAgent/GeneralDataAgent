"""Atomic parent/child resource reservations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from generaldataagent.contracts.child_budget import (
    BudgetReservationAmount,
    BudgetUsageAmount,
    ChildBudgetReservation,
    ChildBudgetSettlement,
    ChildBudgetState,
    child_budget_reservation_payload,
    child_budget_settlement_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash


class ChildBudgetRefused(ValueError):
    """A child budget operation was denied without degrading."""


class ChildBudgetBook:
    """One ledger row per reservation, with reservation and settlement as data."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.clock = path, clock or (lambda: datetime.now(UTC))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ChildBudgetRefused("child_budget_schema_unsupported")
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ChildBudgetRefused("child_budget_dedicated_database_required")
                db.execute(
                    "CREATE TABLE child_budgets ("
                    "key TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL, child_id TEXT NOT NULL, "
                    "state TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)"
                )
                db.execute("PRAGMA user_version = 1")

    @staticmethod
    def _name(value: object) -> bool:
        return type(value) is str and bool(re.fullmatch(r"^[a-z][a-z0-9_.-]{0,127}$", value))

    def _now(self) -> float:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ChildBudgetRefused("child_budget_clock_naive")
        return now.timestamp()

    @staticmethod
    def _key(parent_task_id: UUID, child_id: str) -> str:
        return canonical_hash({"parent_task_id": str(parent_task_id), "child_id": child_id})

    @staticmethod
    def _row_state(row) -> ChildBudgetState:
        return ChildBudgetState.model_validate_json(row[4])

    @staticmethod
    def _digest(
        parent_task_id: UUID,
        child_id: str,
        reservation_id: str,
        amount: BudgetReservationAmount,
    ) -> str:
        return canonical_hash(
            child_budget_reservation_payload(
                parent_task_id=parent_task_id,
                child_id=child_id,
                reservation_id=reservation_id,
                reservation=amount,
            )
        )

    def reserve_many(
        self,
        parent_task_id: UUID,
        items: dict[str, tuple[str, BudgetReservationAmount]],
    ) -> dict[str, ChildBudgetReservation]:
        if (
            type(parent_task_id) is not UUID
            or not isinstance(items, dict)
            or not items
            or len(items) > 2
            or any(
                not self._name(child_id)
                or not self._name(reservation_id)
                or not isinstance(amount, BudgetReservationAmount)
                for child_id, (reservation_id, amount) in items.items()
            )
        ):
            raise ChildBudgetRefused("child_budget_input_invalid")
        child_ids = list(items)
        keys = [self._key(parent_task_id, child) for child in child_ids]
        digests = {
            child: self._digest(
                parent_task_id,
                child,
                reservation_id,
                amount,
            )
            for child, (reservation_id, amount) in items.items()
        }
        created = self._now()
        results = {}
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for child, key in zip(child_ids, keys, strict=True):
                reservation_id, amount = items[child]
                digest = digests[child]
                row = db.execute(
                    "SELECT key,payload FROM child_budgets WHERE key=?", (key,)
                ).fetchone()
                if row is not None:
                    existing = ChildBudgetState.model_validate_json(row[1])
                    if existing.reservation_digest != digest:
                        raise ChildBudgetRefused("child_budget_reservation_conflict")
                    if existing.state != "active":
                        raise ChildBudgetRefused("child_budget_closed")
                    if existing.status in {
                        "settled",
                        "released",
                        "failed",
                        "unknown",
                    }:
                        raise ChildBudgetRefused("child_budget_terminal")
                else:
                    state = ChildBudgetState(
                        parent_task_id=parent_task_id,
                        child_id=child,
                        reservation_id=reservation_id,
                        state="active",
                        status="reserved",
                        reservation=amount,
                        reservation_digest=digest,
                        usage=BudgetUsageAmount(source="unknown"),
                        retry_count=0,
                        generation=0,
                    )
                    db.execute(
                        "INSERT INTO child_budgets VALUES(?,?,?,?,?,?)",
                        (
                            key,
                            str(parent_task_id),
                            child,
                            "active",
                            state.model_dump_json(),
                            created,
                        ),
                    )
                results[child] = ChildBudgetReservation(
                    parent_task_id=parent_task_id,
                    child_id=child,
                    reservation_id=reservation_id,
                    state="active",
                    reservation=amount,
                    reservation_digest=digest,
                )
        return results

    def reserve(
        self,
        parent_task_id: UUID,
        child_id: str,
        reservation_id: str,
        amount: BudgetReservationAmount,
    ) -> ChildBudgetReservation:
        return self.reserve_many(parent_task_id, {child_id: (reservation_id, amount)})[child_id]

    @staticmethod
    def _exceeds_reservation(reserved: BudgetReservationAmount, usage: BudgetUsageAmount) -> bool:
        pairs = (
            (usage.tokens, reserved.tokens),
            (usage.tool_calls, reserved.tool_calls),
            (usage.scan_bytes, reserved.scan_bytes),
            (usage.cost_microunits, reserved.cost_microunits),
            (usage.runtime_milliseconds, reserved.runtime_milliseconds),
        )
        return any(value is not None and value > bound for value, bound in pairs)

    def record_telemetry_many(
        self,
        parent_task_id: UUID,
        items: dict[str, tuple[str, BudgetUsageAmount, int]],
    ) -> dict[str, ChildBudgetState]:
        """Record partial observations for a stage in one transaction."""
        if (
            type(parent_task_id) is not UUID
            or not isinstance(items, dict)
            or not items
            or len(items) > 2
        ):
            raise ChildBudgetRefused("child_budget_input_invalid")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            updated = {}
            for child, (reservation_digest, usage, retry_count) in items.items():
                if (
                    not self._name(child)
                    or type(reservation_digest) is not str
                    or not isinstance(usage, BudgetUsageAmount)
                    or type(retry_count) is not int
                    or retry_count < 0
                ):
                    raise ChildBudgetRefused("child_budget_input_invalid")
                row = db.execute(
                    "SELECT key,parent_task_id,child_id,state,payload,created "
                    "FROM child_budgets WHERE key=?",
                    (self._key(parent_task_id, child),),
                ).fetchone()
                if row is None:
                    raise ChildBudgetRefused("child_budget_not_found")
                current = self._row_state(row)
                if current.state != "active" or current.status not in {
                    "reserved",
                    "telemetry_recorded",
                }:
                    raise ChildBudgetRefused("child_budget_terminal")
                if current.reservation_digest != reservation_digest:
                    raise ChildBudgetRefused("child_budget_digest_mismatch")
                if self._exceeds_reservation(current.reservation, usage):
                    raise ChildBudgetRefused("child_budget_usage_exceeds_reservation")
                updated[child] = current.model_copy(
                    update={
                        "status": (
                            "telemetry_recorded" if usage.source != "unknown" else current.status
                        ),
                        "usage": usage,
                        "retry_count": retry_count,
                        "generation": current.generation + 1,
                    }
                )
            for child, state in updated.items():
                db.execute(
                    "UPDATE child_budgets SET payload=? WHERE key=?",
                    (state.model_dump_json(), self._key(parent_task_id, child)),
                )
        return updated

    def close_many(
        self,
        parent_task_id: UUID,
        status: str,
        items: dict[str, tuple[str, BudgetUsageAmount]],
    ) -> dict[str, ChildBudgetState]:
        """Close every child in a failed or cancelled stage in one transaction."""
        if (
            type(parent_task_id) is not UUID
            or status not in {"failed", "cancelled"}
            or not isinstance(items, dict)
            or not items
            or len(items) > 2
        ):
            raise ChildBudgetRefused("child_budget_input_invalid")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            updated = {}
            for child, (reservation_digest, usage) in items.items():
                if (
                    not self._name(child)
                    or type(reservation_digest) is not str
                    or not isinstance(usage, BudgetUsageAmount)
                ):
                    raise ChildBudgetRefused("child_budget_input_invalid")
                row = db.execute(
                    "SELECT key,parent_task_id,child_id,state,payload,created "
                    "FROM child_budgets WHERE key=?",
                    (self._key(parent_task_id, child),),
                ).fetchone()
                if row is None:
                    raise ChildBudgetRefused("child_budget_not_found")
                current = self._row_state(row)
                if current.state != "active" or current.status not in {
                    "reserved",
                    "telemetry_recorded",
                }:
                    raise ChildBudgetRefused("child_budget_terminal")
                if current.reservation_digest != reservation_digest:
                    raise ChildBudgetRefused("child_budget_digest_mismatch")
                if self._exceeds_reservation(current.reservation, usage):
                    raise ChildBudgetRefused("child_budget_usage_exceeds_reservation")
                updated[child] = current.model_copy(
                    update={
                        "state": "cancelled" if status == "cancelled" else "active",
                        "status": status,
                        "usage": usage,
                        "generation": current.generation + 1,
                    }
                )
            for child, state in updated.items():
                db.execute(
                    "UPDATE child_budgets SET payload=? WHERE key=?",
                    (state.model_dump_json(), self._key(parent_task_id, child)),
                )
        return updated

    def settle_many(
        self,
        parent_task_id: UUID,
        items: dict[str, tuple[str, BudgetUsageAmount, int]],
    ) -> dict[str, ChildBudgetSettlement]:
        """Settle all children of a completed stage in one transaction."""
        if (
            type(parent_task_id) is not UUID
            or not isinstance(items, dict)
            or not items
            or len(items) > 2
        ):
            raise ChildBudgetRefused("child_budget_input_invalid")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            updated = {}
            settlements = {}
            for child, (reservation_digest, usage, retry_count) in items.items():
                if (
                    not self._name(child)
                    or type(reservation_digest) is not str
                    or not isinstance(usage, BudgetUsageAmount)
                    or type(retry_count) is not int
                    or retry_count < 0
                ):
                    raise ChildBudgetRefused("child_budget_input_invalid")
                row = db.execute(
                    "SELECT key,parent_task_id,child_id,state,payload,created "
                    "FROM child_budgets WHERE key=?",
                    (self._key(parent_task_id, child),),
                ).fetchone()
                if row is None:
                    raise ChildBudgetRefused("child_budget_not_found")
                current = self._row_state(row)
                if current.state != "active" or current.status not in {
                    "reserved",
                    "telemetry_recorded",
                }:
                    raise ChildBudgetRefused("child_budget_terminal")
                if current.reservation_digest != reservation_digest:
                    raise ChildBudgetRefused("child_budget_digest_mismatch")
                if not usage.complete or self._exceeds_reservation(current.reservation, usage):
                    raise ChildBudgetRefused(
                        "child_budget_settlement_incomplete_or_over_reservation"
                    )
                settlements[child] = self._settlement(
                    current,
                    status="settled",
                    usage=usage,
                    retry_count=retry_count,
                )
                updated[child] = current.model_copy(
                    update={
                        "status": "settled",
                        "usage": usage,
                        "retry_count": retry_count,
                        "generation": current.generation + 1,
                    }
                )
            for child, state in updated.items():
                db.execute(
                    "UPDATE child_budgets SET payload=? WHERE key=?",
                    (state.model_dump_json(), self._key(parent_task_id, child)),
                )
        return settlements

    def get(self, parent_task_id: UUID, child_id: str) -> ChildBudgetState | None:
        if type(parent_task_id) is not UUID or not self._name(child_id):
            raise ChildBudgetRefused("child_budget_input_invalid")
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute(
                "SELECT key,parent_task_id,child_id,state,payload,created "
                "FROM child_budgets WHERE key=?",
                (self._key(parent_task_id, child_id),),
            ).fetchone()
        return None if row is None else self._row_state(row)

    def record_telemetry(
        self,
        *,
        parent_task_id: UUID,
        child_id: str,
        reservation_digest: str,
        usage: BudgetUsageAmount,
        retry_count: int = 0,
    ) -> ChildBudgetState:
        if (
            type(parent_task_id) is not UUID
            or not self._name(child_id)
            or not isinstance(usage, BudgetUsageAmount)
            or type(retry_count) is not int
            or retry_count < 0
        ):
            raise ChildBudgetRefused("child_budget_input_invalid")
        key = self._key(parent_task_id, child_id)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT key,parent_task_id,child_id,state,payload,created "
                "FROM child_budgets WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                raise ChildBudgetRefused("child_budget_not_found")
            current = self._row_state(row)
            if current.state != "active":
                raise ChildBudgetRefused("child_budget_closed")
            if current.reservation_digest != reservation_digest:
                raise ChildBudgetRefused("child_budget_digest_mismatch")
            values = (
                usage.tokens,
                usage.tool_calls,
                usage.scan_bytes,
                usage.cost_microunits,
                usage.runtime_milliseconds,
            )
            reservation_values = (
                current.reservation.tokens,
                current.reservation.tool_calls,
                current.reservation.scan_bytes,
                current.reservation.cost_microunits,
                current.reservation.runtime_milliseconds,
            )
            if any(
                value is not None and value > bound
                for value, bound in zip(values, reservation_values, strict=True)
            ):
                raise ChildBudgetRefused("child_budget_usage_exceeds_reservation")
            next_state = current.model_copy(
                update={
                    "status": "telemetry_recorded" if usage.source != "unknown" else current.status,
                    "usage": usage,
                    "retry_count": retry_count,
                    "generation": current.generation + 1,
                }
            )
            db.execute(
                "UPDATE child_budgets SET payload=? WHERE key=?",
                (next_state.model_dump_json(), key),
            )
        return next_state.model_copy(update={"parent_task_id": parent_task_id})

    def settle(
        self,
        parent_task_id: UUID,
        child_id: str,
        reservation_digest: str,
        usage: BudgetUsageAmount,
        retry_count: int = 0,
    ) -> ChildBudgetSettlement:
        current = self._require_open(parent_task_id, child_id)
        if (
            type(parent_task_id) is not UUID
            or not self._name(child_id)
            or not isinstance(usage, BudgetUsageAmount)
            or type(retry_count) is not int
            or retry_count < 0
        ):
            raise ChildBudgetRefused("child_budget_input_invalid")
        if current.reservation_digest != reservation_digest:
            raise ChildBudgetRefused("child_budget_digest_mismatch")
        if current.state != "active":
            raise ChildBudgetRefused("child_budget_closed")
        if not usage.complete:
            raise ChildBudgetRefused("child_budget_settlement_incomplete_or_over_reservation")
        settlement = self._settlement(
            current,
            status="settled",
            usage=usage,
            retry_count=retry_count,
        )
        amount = usage
        reserved = current.reservation
        pairs = (
            (amount.tokens, reserved.tokens),
            (amount.tool_calls, reserved.tool_calls),
            (amount.scan_bytes, reserved.scan_bytes),
            (amount.cost_microunits, reserved.cost_microunits),
            (amount.runtime_milliseconds, reserved.runtime_milliseconds),
        )
        if not amount.complete or any(value is None or value > bound for value, bound in pairs):
            raise ChildBudgetRefused("child_budget_settlement_incomplete_or_over_reservation")
        updated = current.model_copy(
            update={
                "status": "settled",
                "usage": usage,
                "retry_count": retry_count,
                "generation": current.generation + 1,
            }
        )
        self._write_state(parent_task_id, child_id, updated)
        return settlement

    def release(
        self, parent_task_id: UUID, child_id: str, reservation_digest: str
    ) -> ChildBudgetSettlement:
        current = self._require_open(parent_task_id, child_id)
        if current.reservation_digest != reservation_digest:
            raise ChildBudgetRefused("child_budget_digest_mismatch")
        if current.state != "active":
            raise ChildBudgetRefused("child_budget_closed")
        settlement = self._settlement(
            current,
            status="released",
            usage=BudgetUsageAmount(source="unknown"),
            retry_count=current.retry_count,
        )
        updated = current.model_copy(
            update={
                "status": "released",
                "generation": current.generation + 1,
            }
        )
        self._write_state(parent_task_id, child_id, updated)
        return settlement

    def cancel(self, parent_task_id: UUID, child_id: str) -> ChildBudgetState:
        current = self._require(parent_task_id, child_id)
        if current.state != "active" or current.status in {
            "settled",
            "released",
            "failed",
            "unknown",
        }:
            raise ChildBudgetRefused("child_budget_close_conflict")
        updated = current.model_copy(
            update={
                "state": "cancelled",
                "status": "cancelled",
                "generation": current.generation + 1,
            }
        )
        self._write_state(parent_task_id, child_id, updated)
        return updated.model_copy(update={"parent_task_id": parent_task_id})

    def fail(
        self,
        parent_task_id: UUID,
        child_id: str,
        reservation_digest: str,
        usage: BudgetUsageAmount,
    ) -> ChildBudgetSettlement:
        current = self._require_open(parent_task_id, child_id)
        if current.reservation_digest != reservation_digest:
            raise ChildBudgetRefused("child_budget_digest_mismatch")
        if current.state != "active":
            raise ChildBudgetRefused("child_budget_closed")
        if usage.source != "unknown" or usage.complete:
            raise ChildBudgetRefused("child_budget_failed_usage_not_unknown")
        settlement = self._settlement(
            current,
            status="failed",
            usage=usage,
            retry_count=current.retry_count,
        )
        updated = current.model_copy(
            update={
                "status": "failed",
                "usage": usage,
                "generation": current.generation + 1,
            }
        )
        self._write_state(parent_task_id, child_id, updated)
        return settlement

    def unknown(
        self, parent_task_id: UUID, child_id: str, reservation_digest: str
    ) -> ChildBudgetSettlement:
        current = self._require_open(parent_task_id, child_id)
        if current.reservation_digest != reservation_digest:
            raise ChildBudgetRefused("child_budget_digest_mismatch")
        if current.state != "active":
            raise ChildBudgetRefused("child_budget_closed")
        settlement = self._settlement(
            current,
            status="unknown",
            usage=BudgetUsageAmount(source="unknown"),
            retry_count=current.retry_count,
        )
        updated = current.model_copy(
            update={
                "status": "unknown",
                "usage": BudgetUsageAmount(source="unknown"),
                "generation": current.generation + 1,
            }
        )
        self._write_state(parent_task_id, child_id, updated)
        return settlement

    @staticmethod
    def _settlement(
        current: ChildBudgetState,
        *,
        status: str,
        usage: BudgetUsageAmount,
        retry_count: int,
    ) -> ChildBudgetSettlement:
        digest = canonical_hash(
            child_budget_settlement_payload(
                parent_task_id=current.parent_task_id,
                child_id=current.child_id,
                reservation_id=current.reservation_id,
                reservation=current.reservation,
                reservation_digest=current.reservation_digest,
                status=status,
                usage=usage,
                retry_count=retry_count,
            )
        )
        return ChildBudgetSettlement(
            parent_task_id=current.parent_task_id,
            child_id=current.child_id,
            reservation_id=current.reservation_id,
            reservation_digest=current.reservation_digest,
            status=status,
            usage=usage,
            retry_count=retry_count,
            settlement_digest=digest,
        )

    def _require(self, parent_task_id: UUID, child_id: str) -> ChildBudgetState:
        current = self.get(parent_task_id, child_id)
        if current is None:
            raise ChildBudgetRefused("child_budget_not_found")
        return current.model_copy(update={"parent_task_id": parent_task_id})

    def _require_open(self, parent_task_id: UUID, child_id: str) -> ChildBudgetState:
        current = self._require(parent_task_id, child_id)
        if current.state != "active":
            raise ChildBudgetRefused("child_budget_closed")
        if current.status in {"settled", "released", "failed", "unknown"}:
            raise ChildBudgetRefused("child_budget_terminal")
        if current.status not in {"reserved", "telemetry_recorded"}:
            raise ChildBudgetRefused("child_budget_terminal")
        return current

    def _write_state(self, parent_task_id: UUID, child_id: str, state: ChildBudgetState) -> None:
        key = self._key(parent_task_id, child_id)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM child_budgets WHERE key=?", (key,)).fetchone()
            if row is None:
                raise ChildBudgetRefused("child_budget_not_found")
            db.execute(
                "UPDATE child_budgets SET payload=? WHERE key=?",
                (
                    state.model_copy(update={"parent_task_id": parent_task_id}).model_dump_json(),
                    key,
                ),
            )
