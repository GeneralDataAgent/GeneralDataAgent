import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from test_metric_analysis import actor
from test_stage5_actions import intent
from test_stage5_feishu import FakeFeishu
from test_stage5_report_refresh import setup_refresh

from generaldataagent.business.actions import BusinessActionRuntime
from generaldataagent.business.feishu import (
    FeishuReportBackend,
    FeishuTarget,
    RecallConfirmationRequired,
)
from generaldataagent.contracts.business import ActionIntent, ReportDraft


def connected_report(tmp_path):
    scheduler, spec, token, refresher, approvals = setup_refresh(tmp_path)
    transport = FakeFeishu()
    backend = FeishuReportBackend(
        target=FeishuTarget(
            app_id="cli_fixture",
            sender_id="ou_bot",
            receiver_type="chat_id",
            receiver_id="oc_fixture",
            configuration_version="reports-v1",
        ),
        transport=transport,
        admitted=lambda: True,
    )
    previous = scheduler.actions
    scheduler.actions = BusinessActionRuntime(
        previous.path,
        broker=previous.broker,
        approvals=approvals,
        controls=tuple(previous.controls.values()),
        artifact_reader=refresher.read_artifact,
        feishu_backends={("report", "report"): backend},
        clock=lambda: spec.starts_at + timedelta(seconds=1),
    )
    return scheduler, spec, token, refresher, approvals, transport, backend


def propose_report(connected):
    scheduler, spec, token, *_ = connected
    view = scheduler.tick(spec.schedule_id, token)["drafts"][0]
    return view, ActionIntent.model_validate(view["intent"])


def test_report_publication_exact_approved_text_receipt_and_restart(tmp_path):
    connected = connected_report(tmp_path)
    scheduler, _, token, refresher, approvals, transport, backend = connected
    view, action = propose_report(connected)
    runtime = scheduler.actions
    assert action.payload.publication_text.startswith("Orders report\n周期：")
    assert '"rows": [["store", 1], ["web", 2]]' in action.payload.publication_text
    assert "证据" in action.payload.publication_text
    assert action.payload.report_hash in backend.wire_text(action)
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    assert transport.sends == 0
    approvals.approve(UUID(view["approval_id"]), approver_id="actual-test-reviewer")
    result = runtime.execute(action.action_id, token)
    assert result["receipt"]["external_delivery"] is True
    message_id = result["receipt"]["external"]["message_id"]
    assert transport.messages[message_id]["content"]["text"] == backend.wire_text(action)
    restarted = BusinessActionRuntime(
        runtime.path,
        broker=runtime.broker,
        approvals=approvals,
        controls=tuple(runtime.controls.values()),
        artifact_reader=refresher.read_artifact,
        feishu_backends={("report", "report"): backend},
        clock=runtime.clock,
    )
    assert restarted.execute(action.action_id, token) == result
    assert transport.sends == 1
    assert restarted.verify_audit()


def test_report_lost_ack_and_artifact_failure_require_read_only_reconciliation(tmp_path):
    connected = connected_report(tmp_path)
    scheduler, _, token, refresher, approvals, transport, _ = connected
    view, action = propose_report(connected)
    runtime = scheduler.actions
    approvals.approve(UUID(view["approval_id"]), approver_id="actual-test-reviewer")
    transport.fail_after_send = True
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    assert runtime.get(action.action_id, token)["state"] == "uncertain"
    with pytest.raises(ValueError, match="reconciliation"):
        runtime.execute(action.action_id, token)
    message_id = next(iter(transport.messages))
    runtime.artifact_reader = lambda ref, identity: b"changed source"
    with pytest.raises(PermissionError, match="artifact_content_changed"):
        runtime.reconcile(action.action_id, token, message_id)
    runtime.artifact_reader = refresher.read_artifact
    assert runtime.reconcile(action.action_id, token, message_id)["state"] == "completed"
    assert transport.sends == 1


def test_changed_artifact_or_egress_authorization_blocks_before_send(tmp_path):
    connected = connected_report(tmp_path)
    scheduler, _, token, refresher, approvals, transport, _ = connected
    view, action = propose_report(connected)
    runtime = scheduler.actions
    approvals.approve(UUID(view["approval_id"]), approver_id="actual-test-reviewer")
    runtime.artifact_reader = lambda ref, identity: b"changed"
    with pytest.raises(PermissionError, match="artifact_content_changed"):
        runtime.execute(action.action_id, token)
    assert transport.sends == 0
    runtime.artifact_reader = refresher.read_artifact
    outsider = runtime.broker.issue(
        user_id="someone-else",
        tenant_id=actor().tenant_id,
        project_id=actor().project_id,
        data_domains=frozenset(actor().data_domains),
        permissions=frozenset({"business:propose"}),
    )
    draft = intent(action.payload)
    with pytest.raises(PermissionError, match="artifact_access_denied"):
        runtime.propose(draft, outsider)
    assert transport.sends == 0


def test_report_text_cannot_be_substituted_before_approval_or_preflight(tmp_path, monkeypatch):
    connected = connected_report(tmp_path)
    scheduler, _, token, _, approvals, transport, _ = connected
    view, action = propose_report(connected)
    runtime = scheduler.actions
    forged = intent(action.payload.model_copy(update={"publication_text": "Invented report"}))
    with pytest.raises(PermissionError, match="publication_content_changed"):
        runtime.propose(forged, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="actual-test-reviewer")
    consume = approvals.consume

    def consume_then_change(*args, **kwargs):
        result = consume(*args, **kwargs)
        # Recheck at the last preflight, even after the single-use grant was consumed.
        runtime.artifact_reader = lambda ref, identity: b"changed after approval consumption"
        return result

    monkeypatch.setattr(approvals, "consume", consume_then_change)
    with pytest.raises(PermissionError, match="artifact_content_changed"):
        runtime.execute(action.action_id, token)
    assert transport.sends == 0
    assert runtime.get(action.action_id, token)["state"] == "uncertain"


def test_report_recall_has_separate_approval_and_explicit_confirmation(tmp_path):
    connected = connected_report(tmp_path)
    scheduler, _, token, _, approvals, transport, backend = connected
    view, action = propose_report(connected)
    runtime = scheduler.actions
    approvals.approve(UUID(view["approval_id"]), approver_id="actual-test-reviewer")
    runtime.execute(action.action_id, token)
    undo = intent(
        action.payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    compensation = runtime.propose(undo, token)
    assert compensation["approval_id"] != view["approval_id"]
    with pytest.raises(PermissionError):
        runtime.execute(undo.action_id, token)
    approvals.approve(UUID(compensation["approval_id"]), approver_id="actual-test-reviewer")
    with pytest.raises(RecallConfirmationRequired):
        runtime.execute(undo.action_id, token)
    assert transport.recalls == 0
    # An uncertain recall cannot be replayed; successful recall is exercised in a fresh run below.
    assert transport.sends == 1


def test_confirmed_report_recall_verified_without_new_send(tmp_path):
    connected = connected_report(tmp_path)
    scheduler, _, token, _, approvals, transport, backend = connected
    view, action = propose_report(connected)
    runtime = scheduler.actions
    approvals.approve(UUID(view["approval_id"]), approver_id="actual-test-reviewer")
    runtime.execute(action.action_id, token)
    undo = intent(
        action.payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    pending = runtime.propose(undo, token)
    approvals.approve(UUID(pending["approval_id"]), approver_id="actual-test-reviewer")
    backend.recall_confirmed = lambda selected: selected.action_id == undo.action_id
    result = runtime.execute(undo.action_id, token)
    assert result["receipt"]["external"]["state"] == "recalled"
    assert transport.sends == transport.recalls == 1


@pytest.mark.parametrize(
    "bad", ["oversize", "classification", "lineage", "task", "schema", "mention"]
)
def test_report_content_and_lineage_fail_closed(tmp_path, bad):
    connected = connected_report(tmp_path)
    _, _, _, refresher, _, transport, backend = connected
    _, action = propose_report(connected)
    raw = refresher.read_artifact(action.payload.report_artifact, actor())
    body = json.loads(raw)
    if bad == "oversize":
        body["delivery"]["insights"] = ["too long " * 2000]
    elif bad == "classification":
        body["delivery"]["evidence"][0]["classification"] = "restricted"
    elif bad == "lineage":
        action = action.model_copy(
            update={
                "payload": action.payload.model_copy(
                    update={
                        "report_artifact": action.payload.report_artifact.model_copy(
                            update={"lineage": ()}
                        )
                    }
                )
            }
        )
    elif bad == "task":
        body["delivery"]["evidence"][0]["task_id"] = str(uuid4())
    elif bad == "mention":
        body["delivery"]["insights"] = ['<at user_id="all"></at>']
    else:
        body["unrecognized"] = "not a frozen report"
    with pytest.raises((ValueError, PermissionError)):
        backend.prepare(action, json.dumps(body).encode())
    assert transport.sends == 0


def test_missing_report_snapshot_is_not_sent_as_a_hash_only_notice(tmp_path):
    scheduler, _, token, _, _, transport, _ = connected_report(tmp_path)
    bare = intent(
        ReportDraft(title="Missing snapshot", report_hash="sha256:" + "a" * 64, period="today")
    )
    with pytest.raises(PermissionError, match="requires_frozen_scoped_artifact"):
        scheduler.actions.propose(bare, token)
    assert transport.sends == 0
