"""Durable, scoped local draft outbox with exact approvals and atomic receipts.

No network adapter is installed. A claim interrupted before a receipt is quarantined
for operator reconciliation, never silently replayed with a consumed approval.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from generaldataagent.contracts import ToolRequest
from generaldataagent.contracts.business import ActionIntent, ActionKind, ActionReceipt
from generaldataagent.policy.approvals import ApprovalStore
from generaldataagent.policy.engine import PolicyEngine, PolicyRule
from generaldataagent.security.dlp import TextDLPScanner
from generaldataagent.security.identity import SessionBroker


def content_hash(value: object) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class ActionControl:
    kind: ActionKind
    destination: str
    owner: str
    tenant_id: str
    project_id: str
    data_domains: frozenset[str]
    enabled: bool = False
    per_minute: int = 10

    def __post_init__(self):
        if not self.owner.strip() or not self.data_domains or not 1 <= self.per_minute <= 1000:
            raise ValueError("action requires an owner, domains and a bounded rate")


class BusinessActionRuntime:
    """Trusted configuration ports are never exposed to agents or HTTP callers.

    The database is protected runtime state. Events hold only hashes and identifiers;
    draft bodies receive DLP checks before persistence and again before delivery.
    """

    def __init__(
        self,
        path: Path,
        *,
        broker: SessionBroker,
        approvals: ApprovalStore,
        controls: tuple[ActionControl, ...] = (),
        policy: PolicyEngine | None = None,
        scanner: TextDLPScanner | None = None,
        clock: Callable[[], datetime] | None = None,
        service_enabled: Callable[[], bool] = lambda: True,
    ):
        if (
            getattr(approvals, "path", None) is not None
            and approvals.path.resolve() == path.resolve()
        ):
            raise ValueError("approval and action databases must be separate")
        self.path, self.broker, self.approvals = path, broker, approvals
        self.controls = {(c.kind, c.destination): c for c in controls}
        if len(self.controls) != len(controls):
            raise ValueError("duplicate action control")
        self.policy = policy or PolicyEngine(
            [
                PolicyRule(
                    "business.draft",
                    "r2",
                    frozenset({"deliver", "compensate"}),
                    frozenset({"reporter"}),
                    requires_approval=True,
                )
            ]
        )
        self.scanner = scanner or TextDLPScanner(max_bytes=65536)
        self.clock, self.service_enabled = clock or (lambda: datetime.now(UTC)), service_enabled
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS business_actions ("
                "id TEXT PRIMARY KEY, user_id TEXT, tenant TEXT, project TEXT, domains TEXT, "
                "idem TEXT, intent TEXT, intent_hash TEXT, request TEXT, approval TEXT, "
                "state TEXT, receipt TEXT, claimed REAL, "
                "UNIQUE(user_id, tenant, project, idem));"
                "CREATE TABLE IF NOT EXISTS business_resources ("
                "tenant TEXT, project TEXT, destination TEXT, revision INTEGER, "
                "action_id TEXT, payload_hash TEXT, active INTEGER, "
                "PRIMARY KEY(tenant, project, destination));"
                "CREATE TABLE IF NOT EXISTS business_events ("
                "seq INTEGER PRIMARY KEY, action_id TEXT, event TEXT, previous TEXT, digest TEXT);"
            )

    def _db(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("action clock must be timezone-aware")
        return now

    def _identity(self, token: str, permission: str):
        identity = self.broker.authenticate(token)
        if permission not in identity.permissions:
            raise PermissionError("business_permission_denied")
        return identity

    def _control(self, intent, identity):
        control = self.controls.get((intent.payload.kind, intent.destination))
        if (
            control is None
            or control.enabled is not True
            or self.service_enabled() is not True
            or (control.tenant_id, control.project_id) != (identity.tenant_id, identity.project_id)
            or not control.data_domains <= identity.data_domains
        ):
            raise PermissionError("business_action_disabled_or_denied")
        return control

    def _row(self, db, action_id, identity):
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM business_actions WHERE id = ?", (str(action_id),)
        ).fetchone()
        if (
            row is None
            or (row["user_id"], row["tenant"], row["project"])
            != (identity.user_id, identity.tenant_id, identity.project_id)
            or not set(json.loads(row["domains"])) <= identity.data_domains
        ):
            raise PermissionError("business_action_access_denied")
        intent = ActionIntent.model_validate_json(row["intent"])
        if content_hash(intent.model_dump(mode="json")) != row["intent_hash"]:
            raise PermissionError("business_intent_integrity_failure")
        request = ToolRequest.model_validate_json(row["request"])
        if (
            request.arguments != intent.model_dump(mode="json")
            or request.user_id != row["user_id"]
            or request.tenant_id != row["tenant"]
            or request.project_id != row["project"]
            or request.data_domains != tuple(json.loads(row["domains"]))
        ):
            raise PermissionError("business_request_integrity_failure")
        return row, intent, request

    @staticmethod
    def _event(db, action_id, event):
        previous = db.execute(
            "SELECT digest FROM business_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous = previous[0] if previous else "genesis"
        digest = content_hash([str(action_id), event, previous])
        db.execute(
            "INSERT INTO business_events(action_id,event,previous,digest) VALUES(?,?,?,?)",
            (str(action_id), event, previous, digest),
        )

    def propose(self, intent: ActionIntent, token: str) -> dict:
        identity = self._identity(token, "business:propose")
        control = self._control(intent, identity)
        now = self._now()
        if intent.created_at > now or not now < intent.expires_at <= now + timedelta(days=7):
            raise ValueError("invalid_action_expiry")
        self.scanner.check(intent.model_dump_json().encode())
        request = ToolRequest(
            task_id=intent.task_id,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            data_domains=tuple(sorted(control.data_domains)),
            agent_role="reporter",
            tool_name="business.draft",
            action=intent.operation,
            resource=intent.destination,
            arguments=intent.model_dump(mode="json"),
            requested_risk="r2",
        )
        if self.policy.evaluate(request).decision != "require_approval":
            raise PermissionError("business_policy_denied")
        digest = content_hash(intent.model_dump(mode="json"))
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, intent_hash FROM business_actions WHERE user_id=? AND tenant=? "
                "AND project=? AND idem=?",
                (identity.user_id, identity.tenant_id, identity.project_id, intent.idempotency_key),
            ).fetchone()
            if row:
                if row[1] != digest:
                    raise ValueError("idempotency_content_conflict")
                return self._view(db, row[0], identity)
            if intent.compensates is not None:
                original, original_intent, _ = self._row(db, intent.compensates, identity)
                if (
                    original["state"] != "completed"
                    or original_intent.operation != "deliver"
                    or original_intent.payload != intent.payload
                    or original_intent.destination != intent.destination
                ):
                    raise ValueError("invalid_compensation_target")
            approval = self.approvals.create_request(
                request,
                impact_scope=f"Local {intent.payload.kind} draft at {intent.destination}",
                cost_estimate="No external API calls",
                classification="internal",
                rollback_plan="Submit a separately approved compensation bound to this receipt",
                expires_at=intent.expires_at,
            )
            db.execute(
                "INSERT INTO business_actions VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',NULL,NULL)",
                (
                    str(intent.action_id),
                    identity.user_id,
                    identity.tenant_id,
                    identity.project_id,
                    json.dumps(sorted(control.data_domains)),
                    intent.idempotency_key,
                    intent.model_dump_json(),
                    digest,
                    request.model_dump_json(),
                    str(approval.approval_id),
                ),
            )
            self._event(db, intent.action_id, "proposed:" + digest)
            return self._view(db, intent.action_id, identity)

    def _view(self, db, action_id, identity):
        row, intent, _ = self._row(db, action_id, identity)
        receipt = ActionReceipt.model_validate_json(row["receipt"]) if row["receipt"] else None
        if receipt and (
            receipt.intent_hash != row["intent_hash"]
            or receipt.payload_hash != content_hash(intent.payload.model_dump(mode="json"))
        ):
            raise PermissionError("business_receipt_integrity_failure")
        return {
            "intent": intent.model_dump(mode="json"),
            "state": row["state"],
            "approval_id": row["approval"],
            "receipt": receipt.model_dump(mode="json") if receipt else None,
        }

    def get(self, action_id: UUID, token: str) -> dict:
        identity = self._identity(token, "business:read")
        with self._db() as db:
            return self._view(db, action_id, identity)

    def cancel(self, action_id: UUID, token: str) -> dict:
        identity = self._identity(token, "business:propose")
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row, _, _ = self._row(db, action_id, identity)
            if row["state"] != "pending":
                raise ValueError("action_not_pending")
            self.approvals.cancel(
                UUID(row["approval"]),
                requester_id=identity.user_id,
                reason="Business draft cancelled",
            )
            db.execute(
                "UPDATE business_actions SET state='cancelled' WHERE id=?", (str(action_id),)
            )
            self._event(db, action_id, "cancelled")
            return self._view(db, action_id, identity)

    def execute(self, action_id: UUID, token: str) -> dict:
        identity = self._identity(token, "actions:resume")
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row, intent, request = self._row(db, action_id, identity)
            control = self._control(intent, identity)
            if set(request.data_domains) != control.data_domains:
                raise PermissionError("business_control_scope_changed")
            if row["state"] == "completed":
                return self._view(db, action_id, identity)
            if row["state"] != "pending":
                raise ValueError("action_requires_reconciliation_or_is_closed")
            if intent.expires_at <= self._now():
                raise PermissionError("action_expired")
            self.scanner.check(intent.model_dump_json().encode())
            if self.policy.evaluate(request).decision != "require_approval":
                raise PermissionError("business_policy_changed")
            self.approvals.validate(UUID(row["approval"]), request)
            recent = db.execute(
                "SELECT COUNT(*) FROM business_actions WHERE tenant=? AND project=? "
                "AND json_extract(intent,'$.destination')=? "
                "AND json_extract(intent,'$.payload.kind')=? AND claimed>?",
                (
                    identity.tenant_id,
                    identity.project_id,
                    intent.destination,
                    intent.payload.kind,
                    self._now().timestamp() - 60,
                ),
            ).fetchone()[0]
            if recent >= control.per_minute:
                raise PermissionError("action_rate_limited")
            self._check_resource(db, intent, identity)
            self.broker.authenticate(token)
            db.execute(
                "UPDATE business_actions SET state='executing',claimed=? WHERE id=?",
                (self._now().timestamp(), str(action_id)),
            )
            self._event(db, action_id, "claimed")
        try:
            self.approvals.consume(UUID(row["approval"]), request)
            # Approval consumption cannot be atomically committed with a remote side effect.
            # Only this transactional local backend is installed; interrupted claims quarantine.
            with self._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                identity = self._identity(token, "actions:resume")
                self._control(intent, identity)
                if intent.expires_at <= self._now():
                    raise PermissionError("action_expired")
                if self.policy.evaluate(request).decision != "require_approval":
                    raise PermissionError("business_policy_changed")
                self._check_resource(db, intent, identity)
                receipt = ActionReceipt(
                    action_id=action_id,
                    intent_hash=row["intent_hash"],
                    payload_hash=content_hash(intent.payload.model_dump(mode="json")),
                    destination=intent.destination,
                    revision=intent.expected_revision + 1,
                    operation=intent.operation,
                )
                db.execute(
                    "INSERT INTO business_resources VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(tenant,project,destination) DO UPDATE SET "
                    "revision=excluded.revision,action_id=excluded.action_id,"
                    "payload_hash=excluded.payload_hash,active=excluded.active",
                    (
                        identity.tenant_id,
                        identity.project_id,
                        intent.destination,
                        receipt.revision,
                        str(action_id),
                        receipt.payload_hash,
                        int(intent.operation == "deliver"),
                    ),
                )
                db.execute(
                    "UPDATE business_actions SET state='completed',receipt=? WHERE id=?",
                    (receipt.model_dump_json(), str(action_id)),
                )
                self._event(
                    db, action_id, "completed:" + content_hash(receipt.model_dump(mode="json"))
                )
                return self._view(db, action_id, identity)
        except Exception:
            with self._db() as db, db:
                db.execute(
                    "UPDATE business_actions SET state='uncertain' WHERE id=? "
                    "AND state='executing'",
                    (str(action_id),),
                )
                self._event(db, action_id, "reconciliation_required")
            raise

    def _check_resource(self, db, intent, identity):
        resource = db.execute(
            "SELECT revision,action_id,active FROM business_resources "
            "WHERE tenant=? AND project=? AND destination=?",
            (identity.tenant_id, identity.project_id, intent.destination),
        ).fetchone()
        revision = resource[0] if resource else 0
        if revision != intent.expected_revision:
            raise ValueError("resource_revision_changed")
        if intent.operation == "compensate" and (
            not resource or resource[1] != str(intent.compensates) or resource[2] != 1
        ):
            raise ValueError("compensation_resource_changed")

    def verify_audit(self) -> bool:
        with self._db() as db:
            previous = "genesis"
            for action_id, event, prev, digest in db.execute(
                "SELECT action_id,event,previous,digest FROM business_events ORDER BY seq"
            ):
                if prev != previous or digest != content_hash([action_id, event, prev]):
                    return False
                previous = digest
        return True
