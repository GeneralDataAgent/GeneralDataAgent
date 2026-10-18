from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.business.actions import ActionControl, BusinessActionRuntime
from generaldataagent.contracts.business import (
    ActionIntent,
    DashboardDraft,
    NotificationDraft,
    RegistrationDraft,
    ReportDraft,
    SchedulePRDraft,
    TicketDraft,
)
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SessionBroker

HASH = "sha256:" + "a" * 64
PAYLOADS = [
    DashboardDraft(title="Revenue", metric_names=("revenue",), evidence_hashes=(HASH,)),
    TicketDraft(title="Review quality", description="Duplicate values in source"),
    SchedulePRDraft(
        pipeline="daily",
        interval_minutes=60,
        rationale="Reduce latency",
        rollback_interval_minutes=1440,
    ),
    RegistrationDraft(experiment_id=uuid4(), model_card_hash=HASH),
    NotificationDraft(message="Report ready for review", evidence_hash=HASH),
    ReportDraft(title="Weekly report", report_hash=HASH, period="2026-W42"),
]


@pytest.fixture
def setup(tmp_path):
    broker = SessionBroker()
    approvals = SQLiteApprovalStore(tmp_path / "approvals.db")
    controls = tuple(
        ActionControl(p.kind, p.kind, "operator", "t", "p", frozenset({"sales"}), enabled=True)
        for p in PAYLOADS
    )
    runtime = BusinessActionRuntime(
        tmp_path / "actions.db", broker=broker, approvals=approvals, controls=controls
    )
    token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    return runtime, approvals, broker, token


def intent(payload=PAYLOADS[0], **kwargs):
    return ActionIntent(
        task_id=uuid4(),
        thread_id="original-thread",
        idempotency_key=str(uuid4()),
        destination=payload.kind,
        payload=payload,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        **kwargs,
    )


def approve(runtime, approvals, token, action):
    result = runtime.propose(action, token)
    approvals.approve(result["approval_id"], approver_id="bob")
    return result


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p.kind)
def test_each_action_exact_approval_restart_retry_and_compensation(setup, payload):
    runtime, approvals, broker, token = setup
    action = intent(payload)
    proposed = runtime.propose(action, token)
    assert runtime.propose(action, token) == proposed
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    from uuid import UUID

    approvals.approve(UUID(proposed["approval_id"]), approver_id="bob")
    receipt = runtime.execute(action.action_id, token)
    assert receipt["receipt"]["external_delivery"] is False
    restarted = BusinessActionRuntime(
        runtime.path, broker=broker, approvals=approvals, controls=tuple(runtime.controls.values())
    )
    assert restarted.execute(action.action_id, token) == receipt
    compensation = intent(
        payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    approved = restarted.propose(compensation, token)
    approvals.approve(UUID(approved["approval_id"]), approver_id="bob")
    undone = restarted.execute(compensation.action_id, token)
    assert undone["receipt"]["revision"] == 2
    assert restarted.verify_audit()


def test_conflicting_key_cancel_and_cross_scope(setup):
    runtime, approvals, broker, token = setup
    action = intent()
    result = runtime.propose(action, token)
    with pytest.raises(ValueError, match="idempotency"):
        runtime.propose(action.model_copy(update={"expected_revision": 1}), token)
    outsider = broker.issue(
        user_id="eve",
        tenant_id="t",
        project_id="other",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:read", "actions:resume"}),
    )
    with pytest.raises(PermissionError):
        runtime.get(action.action_id, outsider)
    runtime.cancel(action.action_id, token)
    from uuid import UUID

    with pytest.raises(PermissionError):
        approvals.approve(UUID(result["approval_id"]), approver_id="bob")
    with pytest.raises(ValueError):
        runtime.execute(action.action_id, token)


def test_concurrent_execute_one_side_effect(setup):
    runtime, approvals, _, token = setup
    action = intent()
    from uuid import UUID

    proposal = runtime.propose(action, token)
    approvals.approve(UUID(proposal["approval_id"]), approver_id="bob")

    def run(_):
        try:
            return runtime.execute(action.action_id, token)["state"]
        except ValueError:
            return "busy"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, range(20)))
    assert "completed" in outcomes
    with runtime._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_resources").fetchone()[0] == 1
        assert db.execute("SELECT revision FROM business_resources").fetchone()[0] == 1
    assert runtime.verify_audit()


def test_switch_policy_resource_rate_and_revocation(setup):
    runtime, approvals, broker, token = setup
    from dataclasses import replace
    from uuid import UUID

    first = intent()
    proposal = runtime.propose(first, token)
    approvals.approve(UUID(proposal["approval_id"]), approver_id="bob")
    key = (first.payload.kind, first.destination)
    control = runtime.controls[key]
    runtime.controls[key] = replace(control, enabled=False)
    with pytest.raises(PermissionError, match="disabled"):
        runtime.execute(first.action_id, token)
    runtime.controls[key] = replace(control, per_minute=1)
    runtime.execute(first.action_id, token)
    second = intent(expected_revision=1)
    proposal = runtime.propose(second, token)
    approvals.approve(UUID(proposal["approval_id"]), approver_id="bob")
    with pytest.raises(PermissionError, match="rate_limited"):
        runtime.execute(second.action_id, token)
    runtime.controls[key] = control
    stale = intent()
    proposal = runtime.propose(stale, token)
    approvals.approve(UUID(proposal["approval_id"]), approver_id="bob")
    with pytest.raises(ValueError, match="revision"):
        runtime.execute(stale.action_id, token)
    broker.revoke(token)
    with pytest.raises(PermissionError):
        runtime.execute(second.action_id, token)


def test_expiry_dlp_and_integrity(setup):
    runtime, _, _, token = setup
    secret = intent(TicketDraft(title="Unsafe", description="password=SuperSecretValue"))
    with pytest.raises(PermissionError, match="dlp"):
        runtime.propose(secret, token)
    action = intent()
    runtime.propose(action, token)
    runtime.clock = lambda: datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(PermissionError, match="expired"):
        runtime.execute(action.action_id, token)
    with runtime._db() as db, db:
        db.execute("UPDATE business_actions SET intent_hash='changed'")
    with pytest.raises(PermissionError, match="integrity"):
        runtime.get(action.action_id, token)
    with runtime._db() as db, db:
        db.execute("UPDATE business_events SET event='changed'")
    assert not runtime.verify_audit()


def test_consumption_failure_quarantines_claim_and_never_replays(setup, monkeypatch):
    runtime, approvals, _, token = setup
    from uuid import UUID

    action = intent()
    proposal = runtime.propose(action, token)
    approvals.approve(UUID(proposal["approval_id"]), approver_id="bob")

    def fail(*args):
        raise ConnectionError("approval service unavailable")

    monkeypatch.setattr(approvals, "consume", fail)
    with pytest.raises(ConnectionError):
        runtime.execute(action.action_id, token)
    assert runtime.get(action.action_id, token)["state"] == "uncertain"
    with pytest.raises(ValueError, match="reconciliation"):
        runtime.execute(action.action_id, token)
    with runtime._db() as db:
        assert db.execute("SELECT COUNT(*) FROM business_resources").fetchone()[0] == 0
    assert runtime.verify_audit()


def test_policy_and_scope_change_block_without_consumption(setup):
    runtime, approvals, _, token = setup
    from dataclasses import replace
    from uuid import UUID

    from generaldataagent.policy.engine import PolicyEngine

    action = intent()
    proposal = runtime.propose(action, token)
    approvals.approve(UUID(proposal["approval_id"]), approver_id="bob")
    policy = runtime.policy
    runtime.policy = PolicyEngine([])
    with pytest.raises(PermissionError, match="policy"):
        runtime.execute(action.action_id, token)
    runtime.policy = policy
    key = (action.payload.kind, action.destination)
    runtime.controls[key] = replace(runtime.controls[key], data_domains=frozenset({"other"}))
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    assert runtime.get(action.action_id, token)["state"] == "pending"


def test_compensation_cannot_retract_newer_revision(setup):
    runtime, approvals, _, token = setup
    from uuid import UUID

    first, second = intent(), intent(expected_revision=1)
    for action in (first, second):
        proposed = runtime.propose(action, token)
        approvals.approve(UUID(proposed["approval_id"]), approver_id="bob")
        runtime.execute(action.action_id, token)
    compensation = intent(operation="compensate", compensates=first.action_id, expected_revision=2)
    proposed = runtime.propose(compensation, token)
    approvals.approve(UUID(proposed["approval_id"]), approver_id="bob")
    with pytest.raises(ValueError, match="resource_changed"):
        runtime.execute(compensation.action_id, token)


def test_contract_revalidation_rejects_model_copy_bypass(setup):
    runtime, _, _, token = setup
    action = intent()
    with pytest.raises(ValueError):
        runtime.propose(action.model_copy(update={"operation": "deploy"}), token)
