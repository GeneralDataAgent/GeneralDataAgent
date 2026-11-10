"""Stage 15.1B checks the deterministic parent/child accounting boundary."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from generaldataagent.contracts.child_budget import (
    BudgetReservationAmount,
    BudgetUsageAmount,
)
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.routing.child_budgets import ChildBudgetBook, ChildBudgetRefused


def amount(**kwargs) -> BudgetReservationAmount:
    values = dict(
        tokens=100,
        tool_calls=10,
        scan_bytes=1000,
        cost_microunits=100,
        runtime_milliseconds=10_000,
    )
    values.update(kwargs)
    return BudgetReservationAmount(**values)


def usage(**kwargs) -> BudgetUsageAmount:
    values = dict(
        source="provider_reported",
        tokens=40,
        tool_calls=3,
        scan_bytes=400,
        cost_microunits=40,
        runtime_milliseconds=1000,
        complete=True,
    )
    values.update(kwargs)
    return BudgetUsageAmount(**values)


def book(tmp_path: Path) -> ChildBudgetBook:
    return ChildBudgetBook(tmp_path / "child-budgets")


def test_reserve_settle_records_complete_measured_usage(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    digest = reservation.reservation_digest

    settlement = ledger.settle(parent, "child-a", digest, usage(), retry_count=1)

    assert settlement.status == "settled"
    assert settlement.usage.complete
    assert ledger.get(parent, "child-a").status == "settled"
    # A settled child cannot be reused or settled again.
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", digest, usage())
    with pytest.raises(ChildBudgetRefused):
        ledger.reserve(parent, "child-a", "res-b", amount(tokens=1))


def test_missing_usage_is_unknown_not_zero(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())

    settlement = ledger.unknown(parent, "child-a", reservation.reservation_digest)

    assert settlement.status == "unknown"
    assert settlement.usage.source == "unknown"
    assert settlement.usage.tokens is None
    assert ledger.get(parent, "child-a").usage.source == "unknown"


def test_partial_telemetry_and_retry_count_do_not_settle(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    telemetry = BudgetUsageAmount(
        source="tool_reported",
        tokens=50,
        tool_calls=None,
        scan_bytes=500,
        cost_microunits=None,
        runtime_milliseconds=None,
        complete=False,
    )

    state = ledger.record_telemetry(
        parent_task_id=parent,
        child_id="child-a",
        reservation_digest=reservation.reservation_digest,
        usage=telemetry,
        retry_count=2,
    )

    assert state.status == "telemetry_recorded"
    assert state.retry_count == 2
    assert state.usage.cost_microunits is None
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", reservation.reservation_digest, telemetry, retry_count=2)

    settlement = ledger.settle(
        parent, "child-a", reservation.reservation_digest, usage(), retry_count=2
    )
    assert settlement.retry_count == 2


def test_release_cancels_and_cannot_reserve_again(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    settlement = ledger.release(parent, "child-a", reservation.reservation_digest)
    assert settlement.status == "released"
    assert settlement.usage.source == "unknown"
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", reservation.reservation_digest, usage())
    with pytest.raises(ChildBudgetRefused):
        ledger.cancel(parent, "child-a")


def test_cancelled_child_cannot_be_run_settled_or_reactivated(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    state = ledger.cancel(parent, "child-a")
    assert state.state == "cancelled"
    assert state.status == "cancelled"
    with pytest.raises(ChildBudgetRefused):
        ledger.record_telemetry(
            parent_task_id=parent,
            child_id="child-a",
            reservation_digest=reservation.reservation_digest,
            usage=usage(),
        )
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", reservation.reservation_digest, usage())
    with pytest.raises(ChildBudgetRefused):
        ledger.reserve(parent, "child-a", "res-a", amount())


def test_digest_and_parent_scope_conflicts_fail_closed(tmp_path):
    ledger = book(tmp_path)
    parent, other_parent = uuid4(), uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    wrong = "sha256:" + "0" * 64

    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", wrong, usage())
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(other_parent, "child-a", reservation.reservation_digest, usage())
    assert ledger.get(parent, "child-a").status == "reserved"


def test_over_reservation_and_partial_settlement_are_refused(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())

    with pytest.raises(ChildBudgetRefused):
        ledger.record_telemetry(
            parent_task_id=parent,
            child_id="child-a",
            reservation_digest=reservation.reservation_digest,
            usage=usage(tokens=101),
        )
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", reservation.reservation_digest, usage(tokens=101))
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(
            parent,
            "child-a",
            reservation.reservation_digest,
            BudgetUsageAmount(
                source="tool_reported",
                tokens=40,
                tool_calls=3,
                scan_bytes=400,
                cost_microunits=40,
                runtime_milliseconds=1000,
                complete=False,
            ),
        )
    assert ledger.get(parent, "child-a").status == "reserved"


def test_failure_keeps_unknown_usage_and_closes(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    settlement = ledger.fail(
        parent,
        "child-a",
        reservation.reservation_digest,
        BudgetUsageAmount(source="unknown"),
    )
    assert settlement.status == "failed"
    assert settlement.usage.source == "unknown"
    with pytest.raises(ChildBudgetRefused):
        ledger.settle(parent, "child-a", reservation.reservation_digest, usage())


def test_storage_requires_dedicated_database(tmp_path):
    path = tmp_path / "not-dedicated"
    from contextlib import closing

    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ChildBudgetRefused):
        ChildBudgetBook(path)


def test_reservation_digest_is_canonical_and_stable(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount(tokens=1))

    assert reservation.reservation_digest == canonical_hash(
        {
            "schema_version": "1.0",
            "parent_task_id": str(parent),
            "child_id": "child-a",
            "reservation_id": "res-a",
            "state": "active",
            "reservation": {
                "tokens": 1,
                "tool_calls": 10,
                "scan_bytes": 1000,
                "cost_microunits": 100,
                "runtime_milliseconds": 10000,
            },
        }
    )
    assert ledger.reserve(parent, "child-a", "res-a", amount(tokens=1)).reservation_digest == (
        reservation.reservation_digest
    )
    with pytest.raises(ChildBudgetRefused):
        ledger.reserve(parent, "child-a", "res-a", amount(tokens=2))


def test_unknown_and_unknown_price_are_not_constructed_as_zero_cost(tmp_path):
    ledger = book(tmp_path)
    parent = uuid4()
    reservation = ledger.reserve(parent, "child-a", "res-a", amount())
    settlement = ledger.unknown(parent, "child-a", reservation.reservation_digest)
    assert settlement.usage.cost_microunits is None
    assert settlement.usage.tokens is None
    assert settlement.usage.complete is False
