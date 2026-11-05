import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from generaldataagent.contracts import RiskLevel, ToolRequest
from generaldataagent.policy.approvals import (
    ApprovalError,
    InMemoryApprovalStore,
    SQLiteApprovalStore,
)

START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


def make_store(kind, tmp_path, clock):
    if kind == "memory":
        return InMemoryApprovalStore(clock=clock)
    return SQLiteApprovalStore(tmp_path / "approvals.db", clock=clock)


def grant_request(store, *, approve=True):
    request = ToolRequest(
        task_id=uuid4(),
        user_id="requester",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role="data_engineer",
        tool_name="csv.maintenance.remove",
        action="remove-expired-source",
        resource="sha256:" + "a" * 64,
        arguments={"inventory_digest": "sha256:" + "a" * 64, "file_ids": []},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    created = store.create_request(
        request,
        impact_scope="Remove reviewed expired CSV sources",
        cost_estimate="One local transactional delete",
        classification="internal",
        rollback_plan="Tombstones retain facts",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    if approve:
        store.approve(created.approval_id, approver_id="reviewer")
    return created


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_trusted_port_returns_the_stored_grant_exactly_once(kind, tmp_path):
    clock = Clock()
    store = make_store(kind, tmp_path, clock)
    created = grant_request(store)

    first = store.consume_granted(created.approval_id)

    assert first.approval_id == created.approval_id
    assert first.request_fingerprint == created.request_fingerprint
    with pytest.raises(ApprovalError, match="already been consumed"):
        store.consume_granted(created.approval_id)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_trusted_port_distinguishes_missing_pending_rejected_and_expired(kind, tmp_path):
    clock = Clock()
    store = make_store(kind, tmp_path, clock)

    with pytest.raises(ApprovalError, match="was not found"):
        store.consume_granted(uuid4())

    pending = grant_request(store, approve=False)
    with pytest.raises(ApprovalError, match="was not found"):
        store.consume_granted(pending.approval_id)

    rejected = grant_request(store, approve=False)
    store.reject(rejected.approval_id, reviewer_id="reviewer", reason="synthetic")
    with pytest.raises(ApprovalError, match="was rejected"):
        store.consume_granted(rejected.approval_id)

    expired = grant_request(store)
    clock.now = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(ApprovalError, match="has expired"):
        store.consume_granted(expired.approval_id)
    clock.now = START


def test_sqlite_trusted_port_serializes_two_concurrent_consumers(tmp_path):
    clock = Clock()
    store = make_store("sqlite", tmp_path, clock)
    created = grant_request(store)
    barrier = Barrier(2)

    def consume():
        barrier.wait()
        try:
            return store.consume_granted(created.approval_id).approval_id
        except ApprovalError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(consume) for _ in range(2)]
        results = [future.result() for future in futures]

    assert results.count(created.approval_id) == 1
    assert results.count("approval grant has already been consumed") == 1
    with sqlite3.connect(tmp_path / "approvals.db") as db:
        consumed = db.execute(
            "SELECT COUNT(*) FROM approvals WHERE consumed_at IS NOT NULL"
        ).fetchone()[0]
    assert consumed == 1
