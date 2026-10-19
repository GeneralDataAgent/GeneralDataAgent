"""Protected single-host feedback governance with authenticated curation and signed snapshots.

Local registries remain ineligible for production. Enrollment, native identity, audit
storage, backups, legal holds and independent release evidence belong to operators.
"""

import base64
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import FeedbackRegistry
from generaldataagent.business.production import QualifiedBusinessActionRuntime
from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.observability.http import AuditHTTPTransport
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.security.admission import FileReleaseAdmission
from generaldataagent.security.identity import SQLiteSessionBroker

TABLES = ("feedback_tasks", "feedback", "feedback_reviews", "feedback_datasets", "feedback_splits")


class QualifiedFeedbackRegistry(FeedbackRegistry):
    def __init__(
        self,
        path,
        *,
        broker: SQLiteSessionBroker,
        audit: VerifiedAuditSink,
        release: FileReleaseAdmission,
        reviewer_admitted,
        configuration_version: str,
    ):
        if (
            not isinstance(broker, SQLiteSessionBroker)
            or not isinstance(audit, VerifiedAuditSink)
            or not isinstance(audit.transport, AuditHTTPTransport)
            or not isinstance(release, FileReleaseAdmission)
            or not configuration_version.strip()
        ):
            raise ValueError(
                "qualified feedback requires shared identity, independent audit and release"
            )
        QualifiedBusinessActionRuntime._secure_path(path)
        self.broker, self.audit, self.release = broker, audit, release
        self.reviewer_admitted, self.configuration_version = (
            reviewer_admitted,
            configuration_version,
        )
        self.lock = Lock()
        if path.resolve() == broker.path.resolve():
            raise ValueError("separate feedback state required")
        if path.exists():
            with closing(sqlite3.connect(path)) as db, db:
                if (
                    db.execute("SELECT 1 FROM sqlite_master WHERE name='feedback'").fetchone()
                    and not db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='feedback_signed_states'"
                    ).fetchone()
                ):
                    raise ValueError("local feedback cannot be promoted")
        super().__init__(path)
        os.chmod(path, 0o600)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS feedback_signed_states "
                "(seq INTEGER PRIMARY KEY,operation TEXT,state_hash TEXT,previous TEXT,"
                "configuration TEXT,canonical TEXT,receipt TEXT)"
            )
        with self._exclusive():
            with closing(sqlite3.connect(path)) as db, db:
                fresh = db.execute("SELECT count(*) FROM feedback_signed_states").fetchone()[0] == 0
            if fresh:
                self._append("initialized", str(uuid4()), "feedback-authority", "system")
            if not self.production_ready():
                raise ValueError("feedback admission or signed state denied")

    def _released(self):
        QualifiedBusinessActionRuntime._secure_path(self.path)
        if not self.release.allowed():
            raise PermissionError("feedback_release_denied")

    @contextmanager
    def _exclusive(self):
        try:
            import fcntl
        except ImportError:
            raise PermissionError("native_feedback_locking_unavailable") from None
        if not self.lock.acquire(blocking=False):
            raise ValueError("feedback_service_busy")
        fd = None
        try:
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            value = os.fstat(fd)
            if (
                value.st_uid != os.getuid()
                or not stat.S_ISREG(value.st_mode)
                or stat.S_IMODE(value.st_mode) != 0o600
                or value.st_nlink != 1
            ):
                raise PermissionError("private feedback lock required")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("feedback_service_busy") from None
            yield
        finally:
            if fd is not None:
                os.close(fd)
            self.lock.release()

    @staticmethod
    def _state(db):
        # The private database may contain redacted examples; only this digest leaves it.
        return content_hash(
            {
                table: sorted(
                    db.execute("SELECT * FROM " + table).fetchall(), key=lambda row: json.dumps(row)
                )
                for table in TABLES
            }
        )

    def _verified(self):
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("BEGIN")
                rows = db.execute(
                    "SELECT operation,state_hash,previous,configuration,canonical,receipt "
                    "FROM feedback_signed_states ORDER BY seq"
                ).fetchall()
                current = self._state(db)
            previous = "genesis"
            if not rows:
                return False
            for operation, state_hash, prior, configuration, canonical, raw in rows:
                receipt = AuditReceipt.model_validate_json(raw)
                body = json.loads(canonical)
                key = self.audit.trusted_keys.get(receipt.signer_id)
                if (
                    prior != previous
                    or key is None
                    or receipt.event_sha256 != hashlib.sha256(canonical.encode()).hexdigest()
                    or str(receipt.event_id) != body["event_id"]
                    or receipt.recorded_at > datetime.now(UTC)
                    or body["payload"].get("request_fingerprint")
                    != content_hash([operation, state_hash, prior, configuration])
                ):
                    return False
                key.verify(
                    base64.b64decode(receipt.signature, validate=True), receipt_statement(receipt)
                )
                previous = content_hash(json.loads(raw))
            return rows[-1][1] == current
        except Exception:
            return False

    def production_ready(self):
        try:
            self._released()
            return self._verified()
        except Exception:
            return False

    def _append(self, operation, task, actor, tenant):
        with closing(sqlite3.connect(self.path)) as db, db:
            state_hash = self._state(db)
            last = db.execute(
                "SELECT receipt FROM feedback_signed_states ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        previous = content_hash(json.loads(last[0])) if last else "genesis"
        configuration = content_hash(
            {
                "revision": self.release.revision,
                "environment": self.release.environment,
                "version": self.configuration_version,
                "audit_endpoint": self.audit.transport.endpoint,
            }
        )
        event = AuditEvent(
            task_id=UUID(str(task)),
            actor_id=actor,
            tenant_id=tenant,
            trace_id=str(task),
            event_type="security.event",
            payload={
                "request_fingerprint": content_hash(
                    [operation, state_hash, previous, configuration]
                )
            },
        )
        canonical = self.audit.canonical(event).decode()
        receipt = self.audit.emit(event)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO feedback_signed_states(operation,state_hash,previous,"
                "configuration,canonical,receipt) VALUES(?,?,?,?,?,?)",
                (
                    operation,
                    state_hash,
                    previous,
                    configuration,
                    canonical,
                    receipt.model_dump_json(),
                ),
            )

    def _mutate(self, operation, task, actor, tenant, call, authorize=lambda: None):
        with self._exclusive():
            self._released()
            if not self._verified():
                raise PermissionError("feedback_signed_state_mismatch")
            authorize()
            self._append(operation + ":requested", task, actor, tenant)
            # Recheck identity/enrollment/release after the audit call.
            self._released()
            authorize()
            result = call()
            # If the final acknowledgement is lost, the changed state no longer matches the last
            # signed snapshot: all later operations fail closed until independent adjudication.
            self._append(operation + ":completed", task, actor, tenant)
            return result

    def register_task(self, task_id, actor, result_hash):
        return self._mutate(
            "task_registered",
            task_id,
            actor.user_id,
            actor.tenant_id,
            lambda: super(QualifiedFeedbackRegistry, self).register_task(
                task_id, actor, result_hash
            ),
        )

    def _identity(self, token, permission):
        identity = self.broker.authenticate(token)
        if permission not in identity.permissions or identity.business_constraint is not None:
            raise PermissionError("feedback_permission_denied")
        return identity

    @staticmethod
    def _actor_matches(identity, actor):
        if (identity.user_id, identity.tenant_id, identity.project_id) != (
            actor.user_id,
            actor.tenant_id,
            actor.project_id,
        ) or not set(actor.data_domains) <= identity.data_domains:
            raise PermissionError("feedback_scope_denied")

    def submit(self, feedback, actor):
        raise PermissionError("authenticated_feedback_submission_required")

    def submit_authenticated(self, feedback, actor, token):
        def authorize():
            self._actor_matches(self._identity(token, "business:propose"), actor)

        authorize()
        return self._mutate(
            "feedback_submitted",
            feedback.task_id,
            actor.user_id,
            actor.tenant_id,
            lambda: super(QualifiedFeedbackRegistry, self).submit(feedback, actor),
            authorize,
        )

    def _scope(self, feedback_id):
        self._released()
        with closing(sqlite3.connect(self.path)) as db, db:
            scope, item = self._feedback(db, feedback_id)
        return json.loads(scope), item

    def _reviewer(self, token, scope, operation):
        identity = self._identity(token, "reviews:submit")
        if (
            (identity.tenant_id, identity.project_id) != (scope["tenant_id"], scope["project_id"])
            or not set(scope["data_domains"]) <= identity.data_domains
            or self.reviewer_admitted(identity.user_id, scope, operation) is not True
        ):
            raise PermissionError("feedback_reviewer_not_enrolled")
        return identity

    def review(self, *args, **kwargs):
        raise PermissionError("authenticated_feedback_review_required")

    def review_authenticated(self, feedback_id, token, *, accepted):
        scope, item = self._scope(feedback_id)
        identity = self._reviewer(token, scope, "review")

        def authorize():
            current = self._reviewer(token, scope, "review")
            if current.user_id == scope["user_id"]:
                raise PermissionError("feedback_self_review_denied")

        return self._mutate(
            "feedback_reviewed",
            item.task_id,
            identity.user_id,
            identity.tenant_id,
            lambda: super(QualifiedFeedbackRegistry, self).review(
                feedback_id, reviewer_id=identity.user_id, accepted=accepted
            ),
            authorize,
        )

    def freeze(self, *args, **kwargs):
        raise PermissionError("authenticated_dataset_curation_required")

    def freeze_authenticated(self, dataset_id, feedback_ids, token, *, split):
        if not 1 <= len(feedback_ids) <= 1000:
            raise ValueError("bounded feedback batch required")
        scope, item = self._scope(feedback_ids[0])
        identity = self._reviewer(token, scope, "curate")

        def authorize():
            self._reviewer(token, scope, "curate")
            with closing(sqlite3.connect(self.path)) as db, db:
                for feedback_id in feedback_ids:
                    current, _ = self._feedback(db, feedback_id)
                    if json.loads(current) != scope:
                        raise PermissionError("mixed_dataset_scope")
                    reviewers = [
                        row[0]
                        for row in db.execute(
                            "SELECT reviewer FROM feedback_reviews WHERE id=?", (str(feedback_id),)
                        )
                    ]
                    if any(
                        self.reviewer_admitted(who, scope, "review") is not True
                        for who in reviewers
                    ):
                        raise PermissionError("feedback_review_enrollment_revoked")

        return self._mutate(
            "dataset_frozen",
            item.task_id,
            identity.user_id,
            identity.tenant_id,
            lambda: super(QualifiedFeedbackRegistry, self).freeze(
                dataset_id, feedback_ids, split=split
            ),
            authorize,
        )

    def read_dataset(self, dataset_id, actor: ActorContext):
        with self._exclusive():
            self._released()
            if not self._verified():
                raise PermissionError("feedback_signed_state_mismatch")
            return super().read_dataset(dataset_id, actor)
