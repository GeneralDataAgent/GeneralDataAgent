"""Durable human reviews with immutable, server-registered task ownership."""

import json
import sqlite3
from pathlib import Path
from uuid import UUID

from generaldataagent.contracts import ActorContext, HumanReview, HumanReviewSummary
from generaldataagent.evaluation.reviews import DuplicateReviewError, summarize_reviews


class ScopedHumanReviewStore:
    """One task UUID has exactly one tenant/project/domain owner across restarts.

    register_task is a trusted runtime port, never a caller-controlled API.
    Existing unscoped JSONL reviews are deliberately not auto-imported.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS review_tasks ("
                "task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                "project_id TEXT NOT NULL, domains TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS scoped_reviews ("
                "task_id TEXT NOT NULL, reviewer_id TEXT NOT NULL, review_json TEXT NOT NULL, "
                "PRIMARY KEY (task_id, reviewer_id), "
                "FOREIGN KEY (task_id) REFERENCES review_tasks(task_id))"
            )

    def register_task(self, task_id: UUID, actor: ActorContext) -> None:
        domains = json.dumps(sorted(set(actor.data_domains)))
        scope = (actor.tenant_id, actor.project_id, domains)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT tenant_id, project_id, domains FROM review_tasks WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            if existing is not None:
                if existing != scope:
                    raise PermissionError("task_scope_conflict")
                return
            db.execute("INSERT INTO review_tasks VALUES (?, ?, ?, ?)", (str(task_id), *scope))

    @staticmethod
    def _authorize(db: sqlite3.Connection, task_id: UUID, actor: ActorContext) -> None:
        row = db.execute(
            "SELECT tenant_id, project_id, domains FROM review_tasks WHERE task_id = ?",
            (str(task_id),),
        ).fetchone()
        if (
            row is None
            or row[:2] != (actor.tenant_id, actor.project_id)
            or not set(json.loads(row[2])) <= set(actor.data_domains)
        ):
            # Do not disclose whether a task in another scope exists.
            raise PermissionError("task_access_denied")

    def submit_scoped(self, review: HumanReview, actor: ActorContext) -> HumanReview:
        if review.reviewer_id != actor.user_id:
            raise PermissionError("reviewer_identity_mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize(db, review.task_id, actor)
            try:
                db.execute(
                    "INSERT INTO scoped_reviews VALUES (?, ?, ?)",
                    (str(review.task_id), review.reviewer_id, review.model_dump_json()),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateReviewError("reviewer already reviewed task") from exc
        return review

    def summarize_scoped(self, task_id: UUID, actor: ActorContext) -> HumanReviewSummary:
        with self._connect() as db:
            self._authorize(db, task_id, actor)
            rows = db.execute(
                "SELECT review_json FROM scoped_reviews WHERE task_id = ? ORDER BY reviewer_id",
                (str(task_id),),
            ).fetchall()
        return summarize_reviews(
            task_id, tuple(HumanReview.model_validate_json(row[0]) for row in rows)
        )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA foreign_keys = ON")
        return db
