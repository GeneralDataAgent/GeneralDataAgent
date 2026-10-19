"""Durable, scoped local draft outbox with exact approvals and atomic receipts.

No network adapter is enabled by default. An explicitly configured receiver is bound
into approval content. Interrupted claims quarantine instead of silently replaying.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from generaldataagent.contracts import ArtifactRef, ToolRequest
from generaldataagent.contracts.business import ActionIntent, ActionKind, ActionReceipt
from generaldataagent.policy.approvals import ApprovalStore
from generaldataagent.policy.engine import PolicyEngine, PolicyRule
from generaldataagent.security.dlp import TextDLPScanner
from generaldataagent.security.identity import SessionBroker, SessionIdentity

if TYPE_CHECKING:
    from generaldataagent.business.feishu import FeishuNotificationBackend


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
        feishu_backends: dict[tuple[str, str], "FeishuNotificationBackend"] | None = None,
        artifact_reader: Callable[[ArtifactRef, SessionIdentity], bytes] | None = None,
    ):
        if (
            getattr(approvals, "path", None) is not None
            and approvals.path.resolve() == path.resolve()
        ):
            raise ValueError("approval and action databases must be separate")
        self.path, self.broker, self.approvals = path, broker, approvals
        self.artifact_reader = artifact_reader
        self.controls = {(c.kind, c.destination): c for c in controls}
        self.feishu_backends = dict(feishu_backends or {})
        if any(
            kind != backend.action_kind or (kind, destination) not in self.controls
            for (kind, destination), backend in self.feishu_backends.items()
        ):
            raise ValueError("Feishu backends require a matching explicit destination control")
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

            columns = {r[1] for r in db.execute("PRAGMA table_info(business_actions)")}
            if "consumed" not in columns:
                db.execute(
                    "ALTER TABLE business_actions ADD COLUMN consumed INTEGER NOT NULL DEFAULT 0"
                )

            if "receiver_hint" not in columns:
                db.execute("ALTER TABLE business_actions ADD COLUMN receiver_hint TEXT")

    def _backend(self, intent):
        backend = self.feishu_backends.get((intent.payload.kind, intent.destination))
        if backend is None and intent.delivery_binding is not None:
            raise PermissionError("external_backend_unavailable")
        if backend is not None and backend.bind(intent) != intent:
            raise PermissionError("external_approved_binding_changed")
        return backend

    def _report_content(self, intent, identity) -> bytes | None:
        if intent.payload.kind == "report" and intent.payload.report_artifact is not None:
            if self.artifact_reader is None:
                raise PermissionError("scoped_report_artifact_reader_required")
            ref = intent.payload.report_artifact
            control = self._control(intent, identity)
            content = self.artifact_reader(
                ref, replace(identity, data_domains=control.data_domains)
            )
            if "sha256:" + hashlib.sha256(content).hexdigest() != ref.content_hash:
                raise PermissionError("report_artifact_content_changed")
            self.scanner.check_export(content, ref.classification)
            return content
        return None

    def _verify_references(self, intent, identity):
        content = self._report_content(intent, identity)
        if (
            identity.business_constraint is not None
            and identity.business_constraint(intent, content, True) is not True
        ):
            raise PermissionError("workload_business_scope_denied")
        backend = self.feishu_backends.get((intent.payload.kind, intent.destination))
        if backend is not None and backend.prepare(intent, content) != intent:
            raise PermissionError("external_report_approved_content_changed")

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
        if (
            identity.business_constraint is not None
            and identity.business_constraint(intent, None, False) is not True
        ):
            raise PermissionError("workload_business_scope_denied")
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
        if content_hash(json.loads(row["intent"])) != row["intent_hash"]:
            raise PermissionError("business_intent_integrity_failure")
        request = ToolRequest.model_validate_json(row["request"])
        if (
            request.arguments != json.loads(row["intent"])
            or request.user_id != row["user_id"]
            or request.tenant_id != row["tenant"]
            or request.project_id != row["project"]
            or request.data_domains != tuple(json.loads(row["domains"]))
        ):
            raise PermissionError("business_request_integrity_failure")
        return row, intent, request

    @staticmethod
    def _event(db, action_id, event):
        if not BusinessActionRuntime._verify_audit_db(db):
            raise PermissionError("business_audit_integrity_failure")
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
        intent = ActionIntent.model_validate_json(intent.model_dump_json())
        identity = self._identity(token, "business:propose")
        control = self._control(intent, identity)
        backend = self.feishu_backends.get((intent.payload.kind, intent.destination))
        if backend is not None:
            intent = backend.prepare(intent, self._report_content(intent, identity))
            intent = backend.bind(intent)
        self._backend(intent)
        self._verify_references(intent, identity)
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
                    _, previous, _ = self._row(db, row[0], identity)
                    if previous != intent:
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
                impact_scope=(
                    f"Feishu {intent.operation}: {intent.payload.kind} to "
                    f"{intent.delivery_binding.receiver_id} as {intent.delivery_binding.identity} "
                    f"({intent.delivery_binding.sender_id})"
                    if intent.delivery_binding is not None
                    else f"Local {intent.payload.kind} draft at {intent.destination}"
                ),
                cost_estimate=(
                    "One Feishu send or recall with bounded identity/readback calls; no model call"
                    if intent.delivery_binding is not None
                    else "No external API calls"
                ),
                classification="internal",
                rollback_plan="Submit a separately approved compensation bound to this receipt",
                expires_at=intent.expires_at,
            )
            db.execute(
                "INSERT INTO business_actions "
                "(id,user_id,tenant,project,domains,idem,intent,intent_hash,"
                "request,approval,state,receipt,claimed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',NULL,NULL)",
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
            receipt.action_id != intent.action_id
            or receipt.destination != intent.destination
            or receipt.operation != intent.operation
            or receipt.revision != intent.expected_revision + 1
            or receipt.intent_hash != row["intent_hash"]
            or receipt.payload_hash != content_hash(json.loads(row["intent"])["payload"])
        ):
            raise PermissionError("business_receipt_integrity_failure")
        if row["state"] == "completed":
            completion = "completed:" + content_hash(json.loads(row["receipt"])) if receipt else ""
            if (
                not receipt
                or db.execute(
                    "SELECT 1 FROM business_events WHERE action_id=? AND event=?",
                    (str(action_id), completion),
                ).fetchone()
                is None
            ):
                raise PermissionError("business_completion_integrity_failure")
        return {
            "intent": intent.model_dump(mode="json"),
            "state": row["state"],
            "approval_id": row["approval"],
            "receiver_hint": json.loads(row["receiver_hint"]) if row["receiver_hint"] else None,
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
            row, intent, _ = self._row(db, action_id, identity)
            if (
                identity.business_constraint is not None
                and identity.business_constraint(intent, None, False) is not True
            ):
                raise PermissionError("workload_business_scope_denied")
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
        with self._db() as db:
            _, preview, _ = self._row(db, action_id, identity)
            backend = self._backend(preview)
        if backend is not None:
            backend.check(preview)
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
            self._verify_references(intent, identity)
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
            busy = db.execute(
                "SELECT 1 FROM business_actions WHERE tenant=? AND project=? AND id<>? "
                "AND json_extract(intent,'$.destination')=? AND state IN ('executing','uncertain')",
                (identity.tenant_id, identity.project_id, str(action_id), intent.destination),
            ).fetchone()
            if busy:
                raise ValueError("action_destination_requires_reconciliation")
            self.broker.authenticate(token)
            db.execute(
                "UPDATE business_actions SET state='executing',claimed=? WHERE id=?",
                (self._now().timestamp(), str(action_id)),
            )
            self._event(db, action_id, "claimed")
        try:
            self.approvals.consume(UUID(row["approval"]), request)
            with self._db() as db, db:
                db.execute("UPDATE business_actions SET consumed=1 WHERE id=?", (str(action_id),))
                self._event(db, action_id, "approval_consumed")
            external = None
            if backend is not None:
                prior = None
                if intent.compensates is not None:
                    with self._db() as db:
                        original, _, _ = self._row(db, intent.compensates, identity)
                        original_receipt = ActionReceipt.model_validate_json(original["receipt"])
                        prior = original_receipt.external
                external = backend.deliver(
                    intent,
                    prior=prior,
                    before_write=lambda: self._preflight_write(action_id, token),
                    record_hint=lambda hint: self._record_receiver_hint(action_id, hint),
                )
            with self._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                identity = self._identity(token, "actions:resume")
                final_control = self._control(intent, identity)
                if set(request.data_domains) != final_control.data_domains:
                    raise PermissionError("business_control_scope_changed")
                if intent.expires_at <= self._now():
                    raise PermissionError("action_expired")
                if self.policy.evaluate(request).decision != "require_approval":
                    raise PermissionError("business_policy_changed")
                self._check_resource(db, intent, identity)
                self._verify_references(intent, identity)
                receipt = ActionReceipt(
                    action_id=action_id,
                    intent_hash=row["intent_hash"],
                    payload_hash=content_hash(request.arguments["payload"]),
                    destination=intent.destination,
                    revision=intent.expected_revision + 1,
                    operation=intent.operation,
                    backend="feishu" if external is not None else "local_draft",
                    external_delivery=external is not None,
                    external=external,
                )
                self._commit_receipt(db, intent, identity, receipt)
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

    def _record_receiver_hint(self, action_id, hint):
        import re

        if not re.fullmatch(
            r"om_[A-Za-z0-9]{1,128}", hint.get("message_id", "")
        ) or not re.fullmatch(r"oc_[A-Za-z0-9]{1,128}", hint.get("chat_id") or ""):
            raise PermissionError("invalid_receiver_hint")
        with self._db() as db, db:
            db.execute(
                "UPDATE business_actions SET receiver_hint=? WHERE id=? AND state='executing'",
                (json.dumps(hint), str(action_id)),
            )
            self._event(db, action_id, "receiver_acknowledged:" + content_hash(hint))

    def _commit_receipt(self, db, intent, identity, receipt):
        self._check_resource(db, intent, identity)
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
                str(intent.action_id),
                receipt.payload_hash,
                int(intent.operation == "deliver"),
            ),
        )
        db.execute(
            "UPDATE business_actions SET state='completed',receipt=? WHERE id=?",
            (receipt.model_dump_json(), str(intent.action_id)),
        )
        self._event(
            db, intent.action_id, "completed:" + content_hash(receipt.model_dump(mode="json"))
        )

    def _preflight_write(self, action_id, token):
        identity = self._identity(token, "actions:resume")
        with self._db() as db:
            row, intent, request = self._row(db, action_id, identity)
            control = self._control(intent, identity)
            backend = self._backend(intent)
            if backend is not None and backend.admitted() is not True:
                raise PermissionError("external_backend_not_admitted")
            if (
                row["state"] != "executing"
                or not row["consumed"]
                or set(request.data_domains) != control.data_domains
                or intent.expires_at <= self._now()
                or self.policy.evaluate(request).decision != "require_approval"
            ):
                raise PermissionError("external_execution_recheck_failed")
            self._check_resource(db, intent, identity)

            self._verify_references(intent, identity)

    def reconcile(self, action_id: UUID, token: str, message_id: str) -> dict:
        identity = self._identity(token, "actions:resume")
        with self._db() as db:
            row, intent, request = self._row(db, action_id, identity)
            self._control(intent, identity)
            backend = self._backend(intent)
            if backend is None:
                raise ValueError("local_action_has_no_external_reconciliation")
            if row["state"] == "completed":
                view = self._view(db, action_id, identity)
                if view["receipt"]["external"]["message_id"] != message_id:
                    raise PermissionError("reconciliation_message_mismatch")
                return view
            crashed_claim = row["state"] == "executing" and intent.expires_at <= self._now()
            if (row["state"] != "uncertain" and not crashed_claim) or not row["consumed"]:
                raise ValueError("action_not_ready_for_reconciliation")
            original_state = row["state"]
            if self.policy.evaluate(request).decision != "require_approval":
                raise PermissionError("business_policy_changed")
            self._verify_references(intent, identity)
        external = backend.reconcile(intent, message_id)
        identity = self._identity(token, "actions:resume")
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row, current, request = self._row(db, action_id, identity)
            self._control(current, identity)
            self._backend(current)
            if row["state"] != original_state or current != intent:
                raise ValueError("reconciliation_state_changed")
            if self.policy.evaluate(request).decision != "require_approval":
                raise PermissionError("business_policy_changed")
            self._verify_references(current, identity)
            receipt = ActionReceipt(
                action_id=action_id,
                intent_hash=row["intent_hash"],
                payload_hash=content_hash(request.arguments["payload"]),
                destination=intent.destination,
                revision=intent.expected_revision + 1,
                operation=intent.operation,
                backend="feishu",
                external_delivery=True,
                external=external,
            )
            self._event(db, action_id, "receiver_reconciled")
            self._commit_receipt(db, intent, identity, receipt)
            return self._view(db, action_id, identity)

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

    @staticmethod
    def _verify_audit_db(db) -> bool:
        previous = "genesis"
        for action_id, event, prev, digest in db.execute(
            "SELECT action_id,event,previous,digest FROM business_events ORDER BY seq"
        ):
            if prev != previous or digest != content_hash([action_id, event, prev]):
                return False
            previous = digest
        return True

    def verify_audit(self) -> bool:
        with self._db() as db:
            return self._verify_audit_db(db)
