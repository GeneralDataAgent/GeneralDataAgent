"""Reviewed experience reuse for recurring analysis failures.

The valuable artefact after a failure is not the question and answer. It is the
pair of (failure class, applicable condition, repair that worked, counterexample
that must still fail). This module stores exactly that and refuses everything
else, so a reuse store cannot quietly become an answer key.

Two hard separations are enforced structurally rather than by convention:

* Evaluation labels and reference SQL never enter the store. The record contract
  has no field for them, and the store rejects content that looks like one.
* Only records that passed independent review are searchable by the runtime.
  Newly captured records land in a candidate area that runtime search never
  reads.

Nothing here calls a model, loads code, or grants a tool. Records are data.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from generaldataagent.business.actions import content_hash
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.dlp import TextDLPScanner

# Content shapes that mean "this is an answer, not a lesson". Rejecting these
# keeps the store from being repurposed as a label cache by a careless caller.
# The SQL shape requires a column list between SELECT and FROM, so ordinary
# guidance such as "select exactly one time scope from month or day_of_year"
# is not mistaken for a query.
_ANSWER_SHAPES = (
    re.compile(
        r"\bselect\s+(?:distinct\s+)?(?:\*|[a-z_][\w.]*(?:\s*,\s*[a-z_][\w.]*)*)\s+from\s+[a-z_]",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:expected|reference|ground[_ ]truth|gold)[_ ]?(?:answer|sql|label)s?\b"),
)


class ExperienceError(ValueError):
    """Stable failure code; never echoes the offending content."""

    CODES = frozenset(
        {
            "experience_contains_answer_material",
            "experience_unknown_record",
            "experience_integrity_failure",
            "experience_scope_mismatch",
            "experience_not_independently_reviewed",
            "experience_already_exists",
            "experience_review_denied",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unknown experience failure code")
        self.code = code
        super().__init__(code)


class ExperienceRecord(ContractModel):
    """One reusable lesson: what failed, when it applies, and how it was fixed.

    There is deliberately no field for a question, an expected answer, a
    reference query or a case identifier. A record that needs those is a
    benchmark artefact, not reusable experience.
    """

    experience_id: UUID = Field(default_factory=uuid4)
    failure_class: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    applies_when: tuple[str, ...] = Field(min_length=1, max_length=10)
    does_not_apply_when: tuple[str, ...] = Field(min_length=1, max_length=10)
    repair: str = Field(min_length=1, max_length=2000)
    counterexample: str = Field(min_length=1, max_length=2000)
    tool_scope: tuple[str, ...] = Field(min_length=1, max_length=20)
    owner: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime

    def text(self) -> str:
        """Everything a reviewer or the DLP scanner needs to see."""
        return json.dumps(
            {
                "failure_class": self.failure_class,
                "applies_when": list(self.applies_when),
                "does_not_apply_when": list(self.does_not_apply_when),
                "repair": self.repair,
                "counterexample": self.counterexample,
                "tool_scope": list(self.tool_scope),
            },
            sort_keys=True,
            ensure_ascii=False,
        )


class ExperienceStore:
    """Scope-bound experience records with review-gated runtime retrieval.

    Records are isolated per actor scope exactly as feedback datasets are. A
    record captured under one tenant/project/data-domain combination is
    invisible to every other.
    """

    def __init__(self, path: Path, *, clock):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self.scanner = TextDLPScanner(max_bytes=65536)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS experiences "
                "(id TEXT PRIMARY KEY, scope TEXT, body TEXT, hash TEXT, state TEXT);"
                "CREATE TABLE IF NOT EXISTS experience_reviews "
                "(id TEXT, reviewer TEXT, accepted INTEGER, PRIMARY KEY(id,reviewer));"
            )

    @staticmethod
    def _scope(actor: ActorContext) -> str:
        value = actor.model_dump(mode="json")
        value["data_domains"] = sorted(set(actor.data_domains))
        return content_hash(value)

    def capture(self, record: ExperienceRecord, actor: ActorContext) -> str:
        """Store a candidate record; runtime search never returns it yet."""
        text = record.text()
        self.scanner.check(text.encode())
        # Inspect every free-text field, not only the rendered brief, so an
        # answer pasted into a tuple member is still rejected.
        inspected = " ".join(
            (text, *record.applies_when, *record.does_not_apply_when, *record.tool_scope)
        )
        for shape in _ANSWER_SHAPES:
            if shape.search(inspected):
                raise ExperienceError("experience_contains_answer_material")
        if any(
            not value.strip() or len(value) > 500
            for value in (*record.applies_when, *record.does_not_apply_when, *record.tool_scope)
        ):
            raise ExperienceError("experience_contains_answer_material")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            try:
                db.execute(
                    "INSERT INTO experiences VALUES(?,?,?,?,'candidate')",
                    (
                        str(record.experience_id),
                        self._scope(actor),
                        record.model_dump_json(),
                        content_hash(record.model_dump(mode="json")),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ExperienceError("experience_already_exists") from exc
        return str(record.experience_id)

    def review(
        self,
        experience_id: UUID,
        *,
        reviewer_id: str,
        accepted: bool,
        actor: ActorContext,
    ) -> None:
        """Independent review; the capturing user may not review their own record."""
        if not reviewer_id.strip() or type(accepted) is not bool:
            raise ExperienceError("experience_review_denied")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            record = self._load(db, experience_id, actor)
            if reviewer_id == record.owner:
                raise ExperienceError("experience_review_denied")
            try:
                db.execute(
                    "INSERT INTO experience_reviews VALUES(?,?,?)",
                    (str(experience_id), reviewer_id, int(accepted)),
                )
            except sqlite3.IntegrityError as exc:
                raise ExperienceError("experience_review_denied") from exc
            if accepted:
                db.execute(
                    "UPDATE experiences SET state='active' WHERE id=?", (str(experience_id),)
                )

    def _load(self, db, experience_id: UUID, actor: ActorContext) -> ExperienceRecord:
        row = db.execute(
            "SELECT scope,body,hash FROM experiences WHERE id=?", (str(experience_id),)
        ).fetchone()
        if row is None or row[0] != self._scope(actor):
            raise ExperienceError("experience_scope_mismatch")
        record = ExperienceRecord.model_validate_json(row[1])
        if content_hash(record.model_dump(mode="json")) != row[2]:
            raise ExperienceError("experience_integrity_failure")
        return record

    def retrieve(
        self,
        *,
        failure_class: str,
        tool: str,
        actor: ActorContext,
        limit: int = 5,
    ) -> tuple[ExperienceRecord, ...]:
        """Reviewed records for one failure class and tool, newest first.

        Candidates and rejected records are never returned, regardless of how
        well they match. Only ``active`` records are visible here.
        """
        if not failure_class.strip() or not tool.strip():
            raise ValueError("experience retrieval requires a failure class and tool")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("experience limit must be between 1 and 50")
        scope = self._scope(actor)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute(
                "SELECT body,hash FROM experiences "
                "WHERE scope=? AND state='active' ORDER BY rowid DESC LIMIT 200",
                (scope,),
            ).fetchall()
        selected = []
        for body, digest in rows:
            record = ExperienceRecord.model_validate_json(body)
            if content_hash(record.model_dump(mode="json")) != digest:
                raise ExperienceError("experience_integrity_failure")
            if record.failure_class != failure_class or tool not in record.tool_scope:
                continue
            selected.append(record)
            if len(selected) == limit:
                break
        return tuple(selected)

    def state(self, experience_id: UUID, actor: ActorContext) -> str:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            self._load(db, experience_id, actor)
            row = db.execute(
                "SELECT state FROM experiences WHERE id=?", (str(experience_id),)
            ).fetchone()
        return row[0]


def experience_brief(records) -> tuple[dict, ...]:
    """Bounded, answer-free prompt material derived from retrieved records."""
    return tuple(
        {
            "failure_class": record.failure_class,
            "applies_when": list(record.applies_when),
            "does_not_apply_when": list(record.does_not_apply_when),
            "repair": record.repair,
        }
        for record in records
    )


def experience_hints(store: ExperienceStore, actor: ActorContext, *, limit: int = 3):
    """Bind one actor scope to a retrieval callable an evaluator can hold.

    The agent receives only ``(failure_class, tool) -> briefs``. It never sees
    the store, the scope or the actor, so a prompt cannot widen its own access.
    """
    if not isinstance(store, ExperienceStore):
        raise ValueError("experience hints require an experience store")
    if not isinstance(actor, ActorContext):
        raise ValueError("experience hints require an actor scope")

    def retrieve(failure_class: str, tool: str) -> tuple[dict, ...]:
        records = store.retrieve(failure_class=failure_class, tool=tool, actor=actor, limit=limit)
        return experience_brief(records)

    return retrieve


__all__ = [
    "ExperienceError",
    "ExperienceRecord",
    "ExperienceStore",
    "experience_brief",
    "experience_hints",
]
