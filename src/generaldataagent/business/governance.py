"""Feedback curation and capability admission. Review ports are trusted operator APIs.

Raw feedback cannot become evaluation/training data automatically. Capability admission
records authorize only pinned metadata, never dynamic code loading or tool permissions.
"""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from generaldataagent.business.actions import content_hash
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.dlp import TextDLPScanner


class Feedback(ContractModel):
    feedback_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    category: Literal["correctness", "grounding", "latency", "usability", "safety"]
    rating: int = Field(ge=1, le=5)
    redacted_question: str = Field(min_length=1, max_length=2000)
    expected_outcome: str = Field(min_length=1, max_length=4000)


class Capability(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    kind: Literal["agent", "skill", "connector", "model"]
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    owner: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    data_domains: tuple[str, ...] = Field(min_length=1, max_length=32)
    regression_evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    security_evidence_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rollback_version: str = Field(min_length=1, max_length=128)
    expires_at: AwareDatetime


def actor_scope(actor: ActorContext) -> str:
    value = actor.model_dump(mode="json")
    value["data_domains"] = sorted(set(actor.data_domains))
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class FeedbackRegistry:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.scanner = TextDLPScanner(max_bytes=65536)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS feedback_tasks "
                "(task TEXT PRIMARY KEY, scope TEXT, hash TEXT);"
                "CREATE TABLE IF NOT EXISTS feedback "
                "(id TEXT PRIMARY KEY, scope TEXT, body TEXT, hash TEXT);"
                "CREATE TABLE IF NOT EXISTS feedback_reviews "
                "(id TEXT, reviewer TEXT, accepted INTEGER, "
                "PRIMARY KEY(id,reviewer));"
                "CREATE TABLE IF NOT EXISTS feedback_datasets "
                "(id TEXT PRIMARY KEY, body TEXT, hash TEXT);"
                "CREATE TABLE IF NOT EXISTS feedback_splits "
                "(task TEXT, result_hash TEXT, split TEXT);"
            )

    def register_task(self, task_id: UUID, actor: ActorContext, result_hash: str) -> None:
        """Trusted runtime hook after a task produces a verified immutable result."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", result_hash):
            raise ValueError("invalid_result_hash")
        scope = actor_scope(actor)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scope,hash FROM feedback_tasks WHERE task=?", (str(task_id),)
            ).fetchone()
            if row is not None and row != (scope, result_hash):
                raise ValueError("feedback_task_registration_conflict")
            db.execute(
                "INSERT OR IGNORE INTO feedback_tasks VALUES(?,?,?)",
                (str(task_id), scope, result_hash),
            )

    def submit(self, feedback: Feedback, actor: ActorContext) -> str:
        self.scanner.check(feedback.model_dump_json().encode())
        scope, body = actor_scope(actor), feedback.model_dump(mode="json")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            row = db.execute(
                "SELECT scope,hash FROM feedback_tasks WHERE task=?", (str(feedback.task_id),)
            ).fetchone()
            if row != (scope, feedback.result_hash):
                raise PermissionError("feedback_task_access_denied")
            try:
                db.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?)",
                    (
                        str(feedback.feedback_id),
                        scope,
                        feedback.model_dump_json(),
                        content_hash(body),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("feedback_already_exists") from exc
        return str(feedback.feedback_id)

    @staticmethod
    def _feedback(db, feedback_id):
        row = db.execute(
            "SELECT scope,body,hash FROM feedback WHERE id=?", (str(feedback_id),)
        ).fetchone()
        if row is None:
            raise ValueError("unknown_feedback")
        item = Feedback.model_validate_json(row[1])
        if content_hash(item.model_dump(mode="json")) != row[2]:
            raise PermissionError("feedback_integrity_failure")
        return row[0], item

    def review(self, feedback_id: UUID, *, reviewer_id: str, accepted: bool) -> None:
        if not reviewer_id.strip() or type(accepted) is not bool:
            raise ValueError("invalid_review")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            scope, _ = self._feedback(db, feedback_id)
            if reviewer_id == json.loads(scope)["user_id"]:
                raise PermissionError("feedback_self_review_denied")
            try:
                db.execute(
                    "INSERT INTO feedback_reviews VALUES(?,?,?)",
                    (str(feedback_id), reviewer_id, int(accepted)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate_feedback_review") from exc

    def freeze(
        self,
        dataset_id: UUID,
        feedback_ids: tuple[UUID, ...],
        *,
        split: Literal["evaluation", "training"],
    ) -> dict:
        if split not in {"evaluation", "training"} or not 1 <= len(feedback_ids) <= 1000:
            raise ValueError("invalid_dataset")
        if len(set(feedback_ids)) != len(feedback_ids):
            raise ValueError("duplicate_feedback")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            examples = []
            dataset_scope = None
            for item_id in sorted(feedback_ids, key=str):
                scope, item = self._feedback(db, item_id)
                if dataset_scope is not None and scope != dataset_scope:
                    raise PermissionError("mixed_dataset_scope")
                dataset_scope = scope
                votes = db.execute(
                    "SELECT accepted FROM feedback_reviews WHERE id=?", (str(item_id),)
                ).fetchall()
                if len(votes) < 2 or not all(v[0] == 1 for v in votes):
                    raise PermissionError("feedback_not_independently_reviewed")
                overlap = db.execute(
                    "SELECT split FROM feedback_splits WHERE "
                    "(task=? OR result_hash=?) AND split<>?",
                    (str(item.task_id), item.result_hash, split),
                ).fetchone()
                if overlap:
                    raise PermissionError("evaluation_training_overlap")
                examples.append(item.model_dump(mode="json"))
                db.execute(
                    "INSERT INTO feedback_splits VALUES(?,?,?)",
                    (str(item.task_id), item.result_hash, split),
                )
            body = {
                "schema_version": "1.0",
                "dataset_id": str(dataset_id),
                "split": split,
                "scope_hash": content_hash(json.loads(dataset_scope)),
                "examples": examples,
            }
            digest = content_hash(body)
            try:
                db.execute(
                    "INSERT INTO feedback_datasets VALUES(?,?,?)",
                    (str(dataset_id), json.dumps(body), digest),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("dataset_is_immutable") from exc
        return {**body, "content_hash": digest}

    def read_dataset(self, dataset_id: UUID, actor: ActorContext) -> dict:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute(
                "SELECT body,hash FROM feedback_datasets WHERE id=?", (str(dataset_id),)
            ).fetchone()
        if row is None:
            raise PermissionError("dataset_access_denied")
        body = json.loads(row[0])
        if body.get("scope_hash") != content_hash(json.loads(actor_scope(actor))):
            raise PermissionError("dataset_access_denied")
        if content_hash(body) != row[1]:
            raise PermissionError("dataset_integrity_failure")
        return {**body, "content_hash": row[1]}


class CapabilityRegistry:
    def __init__(self, path: Path, *, clock):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS capabilities (name TEXT, version TEXT, body TEXT, "
                "hash TEXT, state TEXT, PRIMARY KEY(name,version));"
                "CREATE TABLE IF NOT EXISTS capability_reviews (name TEXT, version TEXT, "
                "reviewer TEXT, discipline TEXT, PRIMARY KEY(name,version,reviewer), "
                "UNIQUE(name,version,discipline));"
            )

    def propose(self, capability: Capability) -> None:
        TextDLPScanner().check(capability.model_dump_json().encode())
        if capability.expires_at <= self.clock():
            raise ValueError("capability_expired")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            try:
                db.execute(
                    "INSERT INTO capabilities VALUES(?,?,?,?,'pending')",
                    (
                        capability.name,
                        capability.version,
                        capability.model_dump_json(),
                        content_hash(capability.model_dump(mode="json")),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("capability_version_is_immutable") from exc

    @staticmethod
    def _row(db, name, version):
        row = db.execute(
            "SELECT body,hash,state FROM capabilities WHERE name=? AND version=?", (name, version)
        ).fetchone()
        if row is None:
            raise PermissionError("capability_not_admitted")
        capability = Capability.model_validate_json(row[0])
        if content_hash(capability.model_dump(mode="json")) != row[1]:
            raise PermissionError("capability_integrity_failure")
        return capability, row[2]

    def review(
        self,
        name: str,
        version: str,
        *,
        reviewer_id: str,
        discipline: Literal["quality", "security"],
    ) -> None:
        if not reviewer_id.strip() or discipline not in {"quality", "security"}:
            raise ValueError("invalid_capability_review")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            capability, state = self._row(db, name, version)
            if state != "pending" or reviewer_id == capability.owner:
                raise PermissionError("capability_review_denied")
            try:
                db.execute(
                    "INSERT INTO capability_reviews VALUES(?,?,?,?)",
                    (name, version, reviewer_id, discipline),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("independent_distinct_reviews_required") from exc

    def activate(self, name: str, version: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            capability, state = self._row(db, name, version)
            disciplines = {
                r[0]
                for r in db.execute(
                    "SELECT discipline FROM capability_reviews WHERE name=? AND version=?",
                    (name, version),
                )
            }
            if (
                state != "pending"
                or capability.expires_at <= self.clock()
                or disciplines != {"quality", "security"}
            ):
                raise PermissionError("capability_admission_denied")
            db.execute(
                "UPDATE capabilities SET state='active' WHERE name=? AND version=?", (name, version)
            )

    def revoke(self, name: str, version: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            self._row(db, name, version)
            db.execute(
                "UPDATE capabilities SET state='revoked' WHERE name=? AND version=?",
                (name, version),
            )

    def require(self, name: str, version: str, digest: str, actor: ActorContext) -> Capability:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            capability, state = self._row(db, name, version)
        if (
            state != "active"
            or capability.expires_at <= self.clock()
            or capability.content_hash != digest
            or (capability.tenant_id, capability.project_id) != (actor.tenant_id, actor.project_id)
            or not set(capability.data_domains) <= set(actor.data_domains)
        ):
            raise PermissionError("capability_not_admitted")
        return capability
