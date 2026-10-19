"""Single-host qualified business assembly with independent approvals and signed audit.

Local pilot instances remain ineligible. Nothing here supplies acceptance evidence,
operator identities, trust keys, retention policy or live receiver enrollment.
"""

import base64
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.business.actions import BusinessActionRuntime, content_hash
from generaldataagent.contracts import AuditEvent, ToolRequest
from generaldataagent.contracts.business import ActionIntent, ActionReceipt
from generaldataagent.observability.http import AuditHTTPTransport
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.policy.approvals import request_fingerprint
from generaldataagent.policy.remote_approvals import HTTPSApprovalTransport, RemoteApprovalStore
from generaldataagent.security.admission import FileReleaseAdmission
from generaldataagent.security.business_admission import FileActionAdmission
from generaldataagent.security.identity import SQLiteSessionBroker


def business_configuration_hash(
    *, control, backend, approval_origin: str, service_version: str, audit
):
    return content_hash(
        {
            "receiver": backend.target.model_dump(mode="json"),
            "kind": control.kind,
            "destination": control.destination,
            "owner": control.owner,
            "tenant": control.tenant_id,
            "project": control.project_id,
            "domains": sorted(control.data_domains),
            "rate": control.per_minute,
            "backend": backend.backend_name,
            "approval_origin": approval_origin,
            "service_version": service_version,
            "audit_endpoint": audit.transport.endpoint,
            "audit_pseudonym_key_hash": hashlib.sha256(audit.pseudonym_key).hexdigest(),
            "audit_keys": {
                name: key.public_bytes_raw().hex()
                for name, key in sorted(audit.trusted_keys.items())
            },
        }
    )


class QualifiedBusinessActionRuntime(BusinessActionRuntime):
    """Requires a fresh qualified state directory; no promotion of a pilot database."""

    def __init__(
        self,
        path: Path,
        *,
        audit: VerifiedAuditSink,
        release: FileReleaseAdmission,
        admissions: dict[tuple[str, str], FileActionAdmission],
        service_version: str,
        **ports,
    ):
        approvals = ports.get("approvals")
        broker = ports.get("broker")
        if (
            not isinstance(audit, VerifiedAuditSink)
            or not isinstance(audit.transport, AuditHTTPTransport)
            or not isinstance(release, FileReleaseAdmission)
            or not isinstance(approvals, RemoteApprovalStore)
            or not isinstance(approvals.transport, HTTPSApprovalTransport)
            or not isinstance(broker, SQLiteSessionBroker)
            or not service_version.strip()
        ):
            raise ValueError(
                "qualified business requires independent approval/audit/release and shared sessions"
            )
        self.audit, self.release, self.admissions = audit, release, dict(admissions)
        self.service_version = service_version
        if path.resolve() == broker.path.resolve():
            raise ValueError("business state and shared session state must be separate")
        self._secure_path(path)
        # Refuse migration of a local pilot history lacking independent acknowledgements.
        if path.exists():
            import sqlite3

            with sqlite3.connect(path) as db:
                present = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='business_events'"
                ).fetchone()
                ack = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='business_audit_receipts'"
                ).fetchone()
                if present and not ack:
                    raise ValueError("pilot business state cannot be promoted")
        super().__init__(path, **ports)
        configured_switch = self.service_enabled
        self.service_enabled = lambda: self.release.allowed() and configured_switch() is True
        os.chmod(path, 0o600)
        with self._db() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS business_audit_receipts "
                "(seq INTEGER PRIMARY KEY,event_id TEXT UNIQUE,canonical TEXT,receipt TEXT)"
            )
        if not self.production_ready():
            raise ValueError("business release or per-destination qualification denied")

    @staticmethod
    def _secure_path(path):
        if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("absolute protected operator state required")
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise PermissionError("private operator state directory required")
        if path.exists():
            value = path.stat()
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != os.getuid()
                or value.st_nlink != 1
                or stat.S_IMODE(value.st_mode) != 0o600
            ):
                raise PermissionError("private regular operator state required")

    def _released(self):
        self._secure_path(self.path)
        if not self.release.allowed():
            raise PermissionError("business_release_admission_denied")

    def _qualified(self, key):
        control = self.controls[key]
        backend = self.feishu_backends.get(key) or self.external_backends.get(key)
        admission = self.admissions.get(key)
        return bool(
            backend is not None
            and isinstance(admission, FileActionAdmission)
            and (admission.kind, admission.destination, admission.owner)
            == (control.kind, control.destination, control.owner)
            and (admission.revision, admission.environment)
            == (self.release.revision, self.release.environment)
            and admission.configuration_hash
            == business_configuration_hash(
                control=control,
                backend=backend,
                approval_origin=self.approvals.transport.origin,
                service_version=self.service_version,
                audit=self.audit,
            )
            and admission.allowed()
        )

    def production_ready(self):
        try:
            self._released()
            enabled = [key for key, control in self.controls.items() if control.enabled]
            with self._db() as db:
                db.execute("BEGIN")
                audit_ok = self._verified_history(db) and self._verified_resources(db)
            return bool(enabled) and audit_ok and all(self._qualified(key) for key in enabled)
        except Exception:
            return False

    def _control(self, intent, identity):
        self._released()
        control = super()._control(intent, identity)
        if not self._qualified((intent.payload.kind, intent.destination)):
            raise PermissionError("business_destination_not_qualified")
        return control

    def _backend(self, intent):
        backend = super()._backend(intent)
        if backend is None:
            raise PermissionError("qualified_business_requires_external_receiver")
        return backend

    def _verified_history(self, db):
        try:
            if not self._verify_audit_db(db):
                return False
            rows = db.execute(
                "SELECT e.seq,e.action_id,e.event,e.previous,e.digest,a.canonical,a.receipt "
                "FROM business_events e LEFT JOIN business_audit_receipts a ON a.seq=e.seq"
            ).fetchall()
            if db.execute("SELECT count(*) FROM business_audit_receipts").fetchone()[0] != len(
                rows
            ):
                return False
            for _seq, action, event, previous, digest, canonical, raw in rows:
                action_row = db.execute(
                    "SELECT intent,intent_hash,request,user_id,tenant,project,domains "
                    "FROM business_actions WHERE id=?",
                    (action,),
                ).fetchone()
                intent = ActionIntent.model_validate_json(action_row[0])
                request = ToolRequest.model_validate_json(action_row[2])
                if (
                    str(intent.action_id) != action
                    or content_hash(intent.model_dump(mode="json")) != action_row[1]
                    or request.arguments != intent.model_dump(mode="json")
                    or (request.user_id, request.tenant_id, request.project_id)
                    != tuple(action_row[3:6])
                    or request.data_domains != tuple(json.loads(action_row[6]))
                    or request.task_id != intent.task_id
                ):
                    return False
                binding = [action, event, previous, digest, request_fingerprint(request)]
                receipt = AuditReceipt.model_validate_json(raw)
                body = json.loads(canonical)
                key = self.audit.trusted_keys.get(receipt.signer_id)
                if (
                    key is None
                    or str(receipt.event_id) != body["event_id"]
                    or receipt.event_sha256 != hashlib.sha256(canonical.encode()).hexdigest()
                    or body["payload"].get("request_fingerprint") != content_hash(binding)
                    or receipt.recorded_at > datetime.now(UTC)
                ):
                    return False
                key.verify(
                    base64.b64decode(receipt.signature, validate=True), receipt_statement(receipt)
                )
            return True
        except Exception:
            return False

    @staticmethod
    def _verified_resources(db):
        try:
            expected = {}
            for action_id, state, consumed in db.execute(
                "SELECT id,state,consumed FROM business_actions"
            ):
                latest = db.execute(
                    "SELECT event FROM business_events WHERE action_id=? ORDER BY seq DESC LIMIT 1",
                    (action_id,),
                ).fetchone()
                if latest is None:
                    return False
                event = latest[0]
                expected_state = (
                    "pending"
                    if event.startswith("proposed:")
                    else "completed"
                    if event.startswith("completed:")
                    else "cancelled"
                    if event == "cancelled"
                    else "uncertain"
                    if event == "reconciliation_required"
                    else "executing"
                    if event in {"claimed", "approval_consumed"}
                    or event.startswith("receiver_acknowledged:")
                    else None
                )
                approval_ack = (
                    db.execute(
                        "SELECT 1 FROM business_events WHERE action_id=? "
                        "AND event='approval_consumed'",
                        (action_id,),
                    ).fetchone()
                    is not None
                )
                if state != expected_state or bool(consumed) != approval_ack:
                    return False
            for action_id, event in db.execute(
                "SELECT action_id,event FROM business_events "
                "WHERE event LIKE 'completed:%' ORDER BY seq"
            ):
                row = db.execute(
                    "SELECT tenant,project,intent,receipt,state FROM business_actions WHERE id=?",
                    (action_id,),
                ).fetchone()
                receipt = ActionReceipt.model_validate_json(row[3])
                intent = ActionIntent.model_validate_json(row[2])
                if (
                    row[4] != "completed"
                    or event != "completed:" + content_hash(json.loads(row[3]))
                    or receipt.action_id != intent.action_id
                    or receipt.destination != intent.destination
                    or receipt.operation != intent.operation
                    or receipt.revision != intent.expected_revision + 1
                    or receipt.intent_hash != content_hash(intent.model_dump(mode="json"))
                    or receipt.payload_hash != content_hash(intent.payload.model_dump(mode="json"))
                ):
                    return False
                expected[(row[0], row[1], receipt.destination)] = (
                    receipt.revision,
                    action_id,
                    receipt.payload_hash,
                    int(receipt.operation == "deliver"),
                )
            actual = {
                tuple(row[:3]): tuple(row[3:])
                for row in db.execute(
                    "SELECT tenant,project,destination,revision,action_id,payload_hash,active "
                    "FROM business_resources"
                )
            }
            return actual == expected
        except Exception:
            return False

    def _event(self, db, action_id, event):
        if not self._verified_history(db):
            raise PermissionError("independent_business_audit_integrity_failure")
        # Append local chain and obtain a signed independent acknowledgement in the same local tx.
        # Remote orphan acknowledgements after rollback are retained by the audit authority.
        super()._event(db, action_id, event)
        seq, previous, digest = db.execute(
            "SELECT seq,previous,digest FROM business_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        row = db.execute(
            "SELECT intent,user_id,tenant,request FROM business_actions WHERE id=?",
            (str(action_id),),
        ).fetchone()
        intent = json.loads(row[0])
        audit_event = AuditEvent(
            event_type="approval.consumed" if event == "approval_consumed" else "security.event",
            task_id=intent["task_id"],
            actor_id=row[1],
            tenant_id=row[2],
            trace_id=str(action_id),
            payload={
                "request_fingerprint": content_hash(
                    [
                        str(action_id),
                        event,
                        previous,
                        digest,
                        request_fingerprint(ToolRequest.model_validate_json(row[3])),
                    ]
                )
            },
        )
        canonical = self.audit.canonical(audit_event).decode()
        receipt = self.audit.emit(audit_event)
        db.execute(
            "INSERT INTO business_audit_receipts VALUES(?,?,?,?)",
            (seq, str(receipt.event_id), canonical, receipt.model_dump_json()),
        )

    def _commit_receipt(self, db, intent, identity, receipt):
        backend = self._backend(intent)
        binding = intent.delivery_binding
        external = receipt.external
        if (
            not receipt.external_delivery
            or receipt.backend != backend.backend_name
            or external is None
            or binding is None
            or external.configuration_hash != binding.configuration_hash
        ):
            raise PermissionError("qualified_receiver_evidence_required")
        if hasattr(external, "binding_hash") and external.binding_hash != content_hash(
            binding.model_dump(mode="json")
        ):
            raise PermissionError("qualified_receiver_binding_mismatch")
        if (
            hasattr(external, "wire_content_hash")
            and external.wire_content_hash != binding.wire_content_hash
        ):
            raise PermissionError("qualified_receiver_content_mismatch")
        return super()._commit_receipt(db, intent, identity, receipt)

    def _view(self, db, action_id, identity):
        view = super()._view(db, action_id, identity)
        proof = db.execute(
            "SELECT a.canonical,a.receipt FROM business_events e "
            "JOIN business_audit_receipts a ON a.seq=e.seq "
            "WHERE e.action_id=? ORDER BY e.seq DESC LIMIT 1",
            (str(action_id),),
        ).fetchone()
        view["audit_acknowledgement"] = {
            "event": json.loads(proof[0]),
            "receipt": json.loads(proof[1]),
        }
        return view

    def _row(self, db, action_id, identity):
        self._released()
        if not db.in_transaction:
            db.execute("BEGIN")
        if not self._verified_history(db) or not self._verified_resources(db):
            raise PermissionError("independent_business_audit_integrity_failure")
        return super()._row(db, action_id, identity)

    def list(self, token, *, before=None, limit=25):
        self._released()
        return super().list(token, before=before, limit=limit)

    def verify_audit(self):
        with self._db() as db:
            db.execute("BEGIN")
            return self._verified_history(db) and self._verified_resources(db)
