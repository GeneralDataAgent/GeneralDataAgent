"""Two-step development smoke: prepare an exact request, then execute after human consent.

This is not production admission or independent external qualification. No tokens or app
secrets are exported. Never pass --execute-confirmed before the human reviews the preview.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from generaldataagent.business.actions import ActionControl, BusinessActionRuntime, content_hash
from generaldataagent.business.feishu import (
    FeishuNotificationBackend,
    FeishuTarget,
    LarkCLITransport,
)
from generaldataagent.contracts.business import ActionIntent, NotificationDraft
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SQLiteSessionBroker

MESSAGE = "GeneralDataAgent 第五期飞书通知连通测试：这是一条测试消息，无需处理。"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--execute-confirmed", action="store_true")
    parser.add_argument("--reconcile-message-id")
    args = parser.parse_args()
    root = args.state_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_path = root / "request.json"
    transport = LarkCLITransport()
    identity = transport.identity()
    user = identity.get("identities", {}).get("user", {})
    bot = identity.get("identities", {}).get("bot", {})
    if user.get("verified") is not True or bot.get("verified") is not True:
        raise PermissionError("verified bot and human recipient identities required")
    if not request_path.exists():
        if args.execute_confirmed or args.reconcile_message_id:
            raise ValueError("prepare and review an exact request before executing")
        target = FeishuTarget(
            app_id=identity["appId"],
            sender_id=bot["openId"],
            receiver_type="open_id",
            receiver_id=user["openId"],
            configuration_version="development-live-smoke-v1",
        )
        request = ActionIntent(
            task_id=uuid4(),
            thread_id="stage5-feishu-notification-smoke",
            idempotency_key=str(uuid4()),
            destination="feishu-self-test",
            payload=NotificationDraft(
                message=MESSAGE, evidence_hash=content_hash({"purpose": "stage5-connectivity-test"})
            ),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        record = {
            "mode": "development_live_smoke",
            "target": target.model_dump(mode="json"),
            "approver_id": user["openId"],
            "intent": request.model_dump(mode="json"),
        }
        request_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        request_path.chmod(0o600)
    record = json.loads(request_path.read_text())
    target = FeishuTarget.model_validate(record["target"])
    request = ActionIntent.model_validate(record["intent"])
    if record["approver_id"] != user["openId"] or target.receiver_id != user["openId"]:
        raise PermissionError("smoke recipient/human approver identity changed")
    broker = SQLiteSessionBroker(root / "sessions.db")
    approvals = SQLiteApprovalStore(root / "approvals.db")
    backend = FeishuNotificationBackend(
        target=target,
        transport=transport,
        admitted=lambda: record["mode"] == "development_live_smoke",
    )
    runtime = BusinessActionRuntime(
        root / "actions.db",
        broker=broker,
        approvals=approvals,
        controls=(
            ActionControl(
                "notification",
                "feishu-self-test",
                user["openId"],
                "feishu-pilot",
                "stage5",
                frozenset({"notifications"}),
                enabled=True,
                per_minute=1,
            ),
        ),
        feishu_backends={("notification", "feishu-self-test"): backend},
    )
    token = broker.issue(
        user_id="dataagent-pilot",
        tenant_id="feishu-pilot",
        project_id="stage5",
        data_domains=frozenset({"notifications"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    try:
        if args.reconcile_message_id:
            result = runtime.reconcile(request.action_id, token, args.reconcile_message_id)
        else:
            result = runtime.propose(request, token)
            if args.execute_confirmed:
                if result["state"] == "pending":
                    # This is the actual verified human who confirmed the prepared content,
                    # never a fabricated independent reviewer or production safety signer.
                    try:
                        approvals.approve(
                            UUID(result["approval_id"]), approver_id=record["approver_id"]
                        )
                    except PermissionError:
                        # A prior explicit confirmation may already have granted this request.
                        with runtime._db() as db:
                            _, _, persisted = runtime._row(
                                db, request.action_id, broker.authenticate(token)
                            )
                        approvals.validate(UUID(result["approval_id"]), persisted)
                result = runtime.execute(request.action_id, token)
        print(
            json.dumps(
                {
                    "mode": record["mode"],
                    "recipient_name": user.get("userName"),
                    "sender_name": bot.get("appName"),
                    "result": result,
                    "wire_text": backend.wire_text(request),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        broker.revoke(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
