"""Digest-bound parent/child budget reservations and settlements.

Stage 15.1B is deliberately a deterministic accounting contract, not a worker
runtime. A parent grants a named child a bounded reservation; the child can be
reserved, settled against complete measured usage, released, cancelled, failed,
or left unknown. Partial measurements may be registered as telemetry, but they
cannot make the child settled or reduce its reservation. Cancelled children can
never regain permission to run. Missing usage remains unknown, never zero.
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import canonical_hash

CHILD_BUDGET_VERSION = "1.0"
STATUS: tuple[str, ...] = (
    "reserved",
    "telemetry_recorded",
    "settled",
    "released",
    "cancelled",
    "failed",
    "unknown",
)

_UsageSource = Literal["provider_reported", "tool_reported", "unknown"]
_SettlementStatus = Literal["settled", "released", "failed", "unknown"]
_STATE = Literal["active", "cancelled"]


def _valid_name(value: str) -> bool:
    return bool(re.fullmatch(r"^[a-z][a-z0-9_.-]{0,127}$", value))


def _amount_values(amount: BudgetReservationAmount) -> dict:
    return {
        "tokens": amount.tokens,
        "tool_calls": amount.tool_calls,
        "scan_bytes": amount.scan_bytes,
        "cost_microunits": amount.cost_microunits,
        "runtime_milliseconds": amount.runtime_milliseconds,
    }


def _usage_values(usage: BudgetUsageAmount) -> dict:
    return {
        "source": usage.source,
        "tokens": usage.tokens,
        "tool_calls": usage.tool_calls,
        "scan_bytes": usage.scan_bytes,
        "cost_microunits": usage.cost_microunits,
        "runtime_milliseconds": usage.runtime_milliseconds,
        "complete": usage.complete,
    }


def child_budget_reservation_payload(
    *,
    parent_task_id: UUID,
    child_id: str,
    reservation_id: str,
    reservation: BudgetReservationAmount,
) -> dict:
    return {
        "schema_version": CHILD_BUDGET_VERSION,
        "parent_task_id": str(parent_task_id),
        "child_id": child_id,
        "reservation_id": reservation_id,
        "state": "active",
        "reservation": _amount_values(reservation),
    }


def child_budget_settlement_payload(
    *,
    parent_task_id: UUID,
    child_id: str,
    reservation_id: str,
    reservation: BudgetReservationAmount,
    reservation_digest: str,
    status: _SettlementStatus,
    usage: BudgetUsageAmount,
    retry_count: int,
) -> dict:
    return {
        "schema_version": CHILD_BUDGET_VERSION,
        "parent_task_id": str(parent_task_id),
        "child_id": child_id,
        "reservation_id": reservation_id,
        "state": "active",
        "reservation": _amount_values(reservation),
        "reservation_digest": reservation_digest,
        "status": status,
        "usage": _usage_values(usage),
        "retry_count": retry_count,
    }


class BudgetReservationAmount(ContractModel):
    tokens: int = Field(default=0, ge=0, le=10**12, strict=True)
    tool_calls: int = Field(default=0, ge=0, le=10**9, strict=True)
    scan_bytes: int = Field(default=0, ge=0, le=10**12, strict=True)
    cost_microunits: int = Field(default=0, ge=0, le=10**15, strict=True)
    runtime_milliseconds: int = Field(default=0, ge=0, le=86_400_000, strict=True)

    @model_validator(mode="after")
    def at_least_one_bound(self):
        if not (
            self.tokens
            or self.tool_calls
            or self.scan_bytes
            or self.cost_microunits
            or self.runtime_milliseconds
        ):
            raise ValueError("child_budget_reservation_empty")
        return self


class BudgetUsageAmount(ContractModel):
    source: _UsageSource
    tokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    tool_calls: int | None = Field(default=None, ge=0, le=10**9, strict=True)
    scan_bytes: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    cost_microunits: int | None = Field(default=None, ge=0, le=10**15, strict=True)
    runtime_milliseconds: int | None = Field(default=None, ge=0, le=86_400_000, strict=True)
    complete: bool = False

    @model_validator(mode="after")
    def complete_values(self):
        values = (
            self.tokens,
            self.tool_calls,
            self.scan_bytes,
            self.cost_microunits,
            self.runtime_milliseconds,
        )
        if self.source == "unknown":
            if any(value is not None for value in values) or self.complete:
                raise ValueError("child_budget_unknown_usage_declared")
        elif self.complete and any(type(value) is not int or value < 0 for value in values):
            raise ValueError("child_budget_usage_incomplete")
        return self


class ChildBudgetReservation(ContractModel):
    parent_task_id: UUID = Field()
    child_id: str
    reservation_id: str
    state: _STATE = "active"
    reservation: BudgetReservationAmount
    reservation_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def reservation_digest_matches(self):
        if not _valid_name(self.child_id) or not _valid_name(self.reservation_id):
            raise ValueError("child_budget_name_invalid")
        if self.state != "active":
            raise ValueError("child_budget_reservation_state_invalid")
        expected = canonical_hash(
            child_budget_reservation_payload(
                parent_task_id=self.parent_task_id,
                child_id=self.child_id,
                reservation_id=self.reservation_id,
                reservation=self.reservation,
            )
        )
        if self.reservation_digest is not None and self.reservation_digest != expected:
            raise ValueError("child_budget_reservation_digest_mismatch")
        return self


class ChildBudgetSettlement(ContractModel):
    parent_task_id: UUID = Field()
    child_id: str
    reservation_id: str
    reservation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: _SettlementStatus
    usage: BudgetUsageAmount
    retry_count: int = Field(default=0, ge=0, le=1_000_000, strict=True)
    settlement_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def settlement_matches(self):
        if not _valid_name(self.child_id) or not _valid_name(self.reservation_id):
            raise ValueError("child_budget_name_invalid")
        if self.status == "settled" and not self.usage.complete:
            raise ValueError("child_budget_settled_usage_incomplete")
        if self.status != "settled" and self.usage.source != "unknown":
            raise ValueError("child_budget_terminal_usage_not_unknown")
        return self


class ChildBudgetState(ContractModel):
    parent_task_id: UUID = Field()
    child_id: str
    reservation_id: str
    state: _STATE = "active"
    status: Literal[
        "reserved",
        "telemetry_recorded",
        "settled",
        "released",
        "cancelled",
        "failed",
        "unknown",
    ] = "reserved"
    reservation: BudgetReservationAmount
    reservation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    usage: BudgetUsageAmount = Field(default_factory=lambda: BudgetUsageAmount(source="unknown"))
    retry_count: int = Field(default=0, ge=0, le=1_000_000, strict=True)
    generation: int = Field(default=0, ge=0, le=2**31 - 1, strict=True)

    @model_validator(mode="after")
    def state_matches(self):
        if not _valid_name(self.child_id) or not _valid_name(self.reservation_id):
            raise ValueError("child_budget_name_invalid")
        expected = canonical_hash(
            child_budget_reservation_payload(
                parent_task_id=self.parent_task_id,
                child_id=self.child_id,
                reservation_id=self.reservation_id,
                reservation=self.reservation,
            )
        )
        if self.reservation_digest != expected:
            raise ValueError("child_budget_reservation_digest_mismatch")
        if self.state == "cancelled" and self.status != "cancelled":
            raise ValueError("child_budget_cancelled_status_mismatch")
        if self.status == "settled" and not self.usage.complete:
            raise ValueError("child_budget_settled_usage_incomplete")
        return self


__all__ = [
    "CHILD_BUDGET_VERSION",
    "STATUS",
    "BudgetReservationAmount",
    "BudgetUsageAmount",
    "ChildBudgetReservation",
    "ChildBudgetSettlement",
    "ChildBudgetState",
    "child_budget_reservation_payload",
    "child_budget_settlement_payload",
]
