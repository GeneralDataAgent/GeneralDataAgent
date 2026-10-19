import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_stage5_actions import HASH, intent

from generaldataagent.business.actions import ActionControl, BusinessActionRuntime, content_hash
from generaldataagent.business.feishu import (
    FeishuNotificationBackend,
    FeishuTarget,
    LarkCLITransport,
    RecallConfirmationRequired,
)
from generaldataagent.contracts.business import ActionReceipt, NotificationDraft
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SessionBroker


class FakeFeishu:
    def __init__(self):
        self.messages, self.sends, self.recalls = {}, 0, 0
        self.sender = "ou_bot"
        self.fail_after_send = False
        self.alter_content = False

    def identity(self):
        return {
            "appId": "cli_fixture",
            "verified": True,
            "identities": {"bot": {"openId": self.sender, "verified": True}},
        }

    def send(self, target, text, key):
        self.sends += 1
        message_id = "om_" + key.replace("-", "")
        self.messages[message_id] = {
            "message_id": message_id,
            "chat_id": "oc_fixture",
            "sender": {"id": self.sender},
            "msg_type": "text",
            "content": {"text": text},
            "deleted": False,
        }
        if self.fail_after_send:
            raise TimeoutError("simulated lost acknowledgement")
        return {"message_id": message_id, "chat_id": "oc_fixture"}

    def fetch(self, message_id, identity):
        message = dict(self.messages[message_id])
        if self.alter_content:
            message["content"] = {"text": "changed"}
        return message

    def recall(self, message_id, identity, *, confirmed):
        assert confirmed is True
        self.recalls += 1
        self.messages[message_id]["deleted"] = True


@pytest.fixture
def connected(tmp_path):
    broker = SessionBroker()
    approvals = SQLiteApprovalStore(tmp_path / "approvals")
    transport = FakeFeishu()
    target = FeishuTarget(
        app_id="cli_fixture",
        sender_id="ou_bot",
        receiver_type="chat_id",
        receiver_id="oc_fixture",
        configuration_version="v1",
    )
    backend = FeishuNotificationBackend(target=target, transport=transport, admitted=lambda: True)
    control = ActionControl(
        "notification", "notification", "owner", "t", "p", frozenset({"sales"}), enabled=True
    )
    runtime = BusinessActionRuntime(
        tmp_path / "actions",
        broker=broker,
        approvals=approvals,
        controls=(control,),
        feishu_backends={("notification", "notification"): backend},
    )
    token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    return runtime, approvals, transport, backend, token


def approved(runtime, approvals, token, action):
    view = runtime.propose(action, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    return view


def notification(**kwargs):
    return intent(
        NotificationDraft(message="Stage 5 test notification", evidence_hash=HASH), **kwargs
    )


def test_external_send_exact_receiver_content_restart_retry_and_compensation(connected):
    runtime, approvals, transport, backend, token = connected
    action = notification()
    proposed = approved(runtime, approvals, token, action)
    assert proposed["intent"]["delivery_binding"]["receiver_id"] == "oc_fixture"
    completed = runtime.execute(action.action_id, token)
    assert completed["receipt"]["external_delivery"] is True
    assert completed["receipt"]["external"]["state"] == "delivered"
    assert runtime.execute(action.action_id, token) == completed
    assert transport.sends == 1
    undo = notification(operation="compensate", compensates=action.action_id, expected_revision=1)
    approved(runtime, approvals, token, undo)
    # An exact action grant does not silently bypass CLI's high-risk human confirmation.
    with pytest.raises(RecallConfirmationRequired):
        runtime.execute(undo.action_id, token)
    assert transport.recalls == 0
    assert runtime.get(undo.action_id, token)["state"] == "uncertain"
    # Reconciliation is read-only: a still-visible message cannot be claimed as recalled.
    with pytest.raises(PermissionError, match="recall_not_verified"):
        runtime.reconcile(undo.action_id, token, completed["receipt"]["external"]["message_id"])
    assert runtime.verify_audit()


def test_explicit_recall_confirmation_and_readback(connected):
    runtime, approvals, transport, backend, token = connected
    action = notification()
    approved(runtime, approvals, token, action)
    runtime.execute(action.action_id, token)
    undo = notification(operation="compensate", compensates=action.action_id, expected_revision=1)
    approved(runtime, approvals, token, undo)
    backend.recall_confirmed = lambda selected: selected.action_id == undo.action_id
    result = runtime.execute(undo.action_id, token)
    assert result["receipt"]["external"]["state"] == "recalled"
    assert transport.recalls == 1 and transport.sends == 1


def test_lost_acknowledgement_quarantine_and_read_only_reconciliation(connected):
    runtime, approvals, transport, backend, token = connected
    action = notification()
    approved(runtime, approvals, token, action)
    transport.fail_after_send = True
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    with pytest.raises(ValueError, match="reconciliation"):
        runtime.execute(action.action_id, token)
    another = notification()
    approved(runtime, approvals, token, another)
    with pytest.raises(ValueError, match="destination"):
        runtime.execute(another.action_id, token)
    message_id = next(iter(transport.messages))
    completed = runtime.reconcile(action.action_id, token, message_id)
    assert completed["state"] == "completed"
    assert runtime.reconcile(action.action_id, token, message_id) == completed
    assert transport.sends == 1


def test_target_identity_content_and_admission_changes_fail_closed(connected):
    runtime, approvals, transport, backend, token = connected
    action = notification()
    approved(runtime, approvals, token, action)
    backend.target = backend.target.model_copy(update={"receiver_id": "oc_other"})
    with pytest.raises(PermissionError, match="binding"):
        runtime.execute(action.action_id, token)
    backend.target = backend.target.model_copy(update={"receiver_id": "oc_fixture"})
    transport.sender = "ou_other"
    with pytest.raises(PermissionError, match="sender"):
        runtime.execute(action.action_id, token)
    transport.sender = "ou_bot"
    backend.admitted = lambda: False
    with pytest.raises(PermissionError, match="admitted"):
        runtime.execute(action.action_id, token)
    backend.admitted = lambda: True
    transport.alter_content = True
    with pytest.raises(PermissionError, match="content"):
        runtime.execute(action.action_id, token)
    assert runtime.get(action.action_id, token)["state"] == "uncertain"


def test_cli_envelope_identity_argv_and_confirmation_gate(monkeypatch):
    transport = LarkCLITransport(executable="/bin/echo")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"ok": True, "data": {"message_id": "om_test"}})
        )

    monkeypatch.setattr("generaldataagent.business.feishu.subprocess.run", run)
    target = FeishuTarget(
        app_id="cli_fixture",
        sender_id="ou_bot",
        receiver_type="chat_id",
        receiver_id="oc_fixture",
        configuration_version="v1",
    )
    text = "literal $(touch /tmp/never) `echo never`\nquotes ' \""
    assert transport.send(target, text, str(uuid4()))["message_id"] == "om_test"
    assert json.loads(calls[0][0][calls[0][0].index("--content") + 1]) == {"text": text}
    assert "shell" not in calls[0][1]
    with pytest.raises(RecallConfirmationRequired):
        transport.recall("om_test", "bot", confirmed=False)
    assert len(calls) == 1

    def risk(args, **kwargs):
        return SimpleNamespace(returncode=10, stdout="", stderr="sensitive diagnostics")

    monkeypatch.setattr("generaldataagent.business.feishu.subprocess.run", risk)
    with pytest.raises(RecallConfirmationRequired):
        transport.recall("om_test", "bot", confirmed=True)

    def bad(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"code": 0, "data": {}}))

    monkeypatch.setattr("generaldataagent.business.feishu.subprocess.run", bad)
    with pytest.raises(PermissionError, match="unavailable"):
        transport.send(target, "test", str(uuid4()))


def test_old_persisted_local_action_and_receipt_remain_readable(tmp_path):
    broker = SessionBroker()
    approvals = SQLiteApprovalStore(tmp_path / "approvals")
    control = ActionControl(
        "notification", "notification", "owner", "t", "p", frozenset({"sales"}), enabled=True
    )
    runtime = BusinessActionRuntime(
        tmp_path / "actions", broker=broker, approvals=approvals, controls=(control,)
    )
    token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    action = notification()
    runtime.propose(action, token)
    from generaldataagent.contracts import ToolRequest

    with runtime._db() as db, db:
        row, _, request = runtime._row(db, action.action_id, broker.authenticate(token))
        old_intent = json.loads(row["intent"])
        del old_intent["delivery_binding"]
        old_request = ToolRequest.model_validate({**request.model_dump(), "arguments": old_intent})
        grant = approvals.create_request(
            old_request,
            impact_scope="Fixture",
            cost_estimate="None",
            classification="internal",
            rollback_plan="Retract",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        approvals.approve(grant.approval_id, approver_id="bob")
        db.execute(
            "UPDATE business_actions SET intent=?,intent_hash=?,request=?,approval=? WHERE id=?",
            (
                json.dumps(old_intent),
                content_hash(old_intent),
                old_request.model_dump_json(),
                str(grant.approval_id),
                str(action.action_id),
            ),
        )
    result = runtime.execute(action.action_id, token)
    assert result["receipt"]["backend"] == "local_draft"
    assert ActionReceipt.model_validate(result["receipt"]).external is None
