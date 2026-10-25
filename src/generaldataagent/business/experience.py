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

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import Feedback, FeedbackRegistry
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
    re.compile(
        r"\b(?:expected|reference|ground[_ ]truth|gold)[_ ]?(?:answer|sql|label)s?\b",
        re.IGNORECASE,
    ),
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
            "experience_owner_mismatch",
            "experience_feedback_source_denied",
            "experience_extraction_source_denied",
            "experience_origin_mismatch",
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


class _FeedbackSource(BaseModel):
    """Stored capture receipt; excluded from runtime lesson prompts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experience_id: UUID
    dataset_id: UUID
    dataset_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    feedback_id: UUID
    task_id: UUID
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    split: Literal["training"]


class _ExtractionSource(BaseModel):
    """Stored extraction receipt; carries digests and steps, never trace prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experience_id: UUID
    template_version: str = Field(min_length=1, max_length=64)
    failure_step: int = Field(ge=1)
    recovery_step: int = Field(ge=1)
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    trace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    documents_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    extractor_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperienceStore:
    """Scope-bound experience records with review-gated runtime retrieval.

    Records are isolated per actor scope exactly as feedback datasets are. A
    record captured under one tenant/project/data-domain combination is
    invisible to every other.
    """

    def __init__(self, path: Path, *, clock, read_only: bool = False):
        if type(read_only) is not bool:
            raise ValueError("experience read_only must be boolean")
        self.path, self.clock, self.read_only = path, clock, read_only
        self.scanner = TextDLPScanner(max_bytes=65536)
        if read_only:
            with closing(self._connect()) as db:
                db.execute("SELECT id,scope,body,hash,state FROM experiences LIMIT 0")
                db.execute("SELECT id,reviewer,accepted FROM experience_reviews LIMIT 0")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS experiences "
                "(id TEXT PRIMARY KEY, scope TEXT, body TEXT, hash TEXT, state TEXT);"
                "CREATE TABLE IF NOT EXISTS experience_reviews "
                "(id TEXT, reviewer TEXT, accepted INTEGER, PRIMARY KEY(id,reviewer));"
                "CREATE TABLE IF NOT EXISTS experience_feedback_sources "
                "(id TEXT PRIMARY KEY, body TEXT, hash TEXT);"
                "CREATE TABLE IF NOT EXISTS experience_origins "
                "(id TEXT PRIMARY KEY, origin TEXT);"
                "CREATE TABLE IF NOT EXISTS experience_extraction_sources "
                "(id TEXT PRIMARY KEY, body TEXT, hash TEXT);"
            )
            # Databases written before provenance existed get a marker derived
            # from their receipts. This runs once per record and never rewrites
            # an existing marker, so a later receipt deletion stays detectable.
            db.execute(
                "INSERT INTO experience_origins(id,origin) SELECT id,"
                " CASE WHEN EXISTS(SELECT 1 FROM experience_feedback_sources s"
                " WHERE s.id=experiences.id) THEN 'feedback'"
                " WHEN EXISTS(SELECT 1 FROM experience_extraction_sources s"
                " WHERE s.id=experiences.id) THEN 'extraction' ELSE 'manual' END"
                " FROM experiences WHERE NOT EXISTS"
                " (SELECT 1 FROM experience_origins o WHERE o.id=experiences.id)"
            )

    def _connect(self):
        if self.read_only:
            return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        return sqlite3.connect(self.path, timeout=5)

    def _require_writable(self):
        if self.read_only:
            raise PermissionError("experience_store_read_only")

    @staticmethod
    def _scope(actor: ActorContext) -> str:
        value = actor.model_dump(mode="json")
        value["data_domains"] = sorted(set(actor.data_domains))
        return content_hash(value)

    def _validate_capture(self, record: ExperienceRecord, actor: ActorContext) -> None:
        self._require_writable()
        if record.owner != actor.user_id:
            raise ExperienceError("experience_owner_mismatch")
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

    def _insert_candidate(
        self, db, record: ExperienceRecord, actor: ActorContext, origin: str
    ) -> None:
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
        # Provenance lives beside the record so a later receipt deletion cannot
        # silently turn feedback-derived material into a manual candidate.
        db.execute(
            "INSERT INTO experience_origins VALUES(?,?)", (str(record.experience_id), origin)
        )

    def capture(self, record: ExperienceRecord, actor: ActorContext) -> str:
        """Store a manual candidate; runtime search never returns it yet."""
        self._validate_capture(record, actor)
        with closing(self._connect()) as db, db:
            self._insert_candidate(db, record, actor, "manual")
        return str(record.experience_id)

    def capture_from_feedback(
        self,
        record: ExperienceRecord,
        actor: ActorContext,
        *,
        registry: FeedbackRegistry,
        dataset_id: UUID,
        feedback_id: UUID,
    ) -> str:
        """Capture a candidate tied to scoped frozen training feedback, without its labels."""
        self._require_writable()
        if not isinstance(registry, FeedbackRegistry):
            raise ValueError("feedback capture requires a feedback registry")
        dataset = registry.read_dataset(dataset_id, actor)
        if dataset.get("split") != "training":
            raise ExperienceError("experience_feedback_source_denied")
        matches = [
            example
            for example in dataset["examples"]
            if example.get("feedback_id") == str(feedback_id)
        ]
        if len(matches) != 1:
            raise ExperienceError("experience_feedback_source_denied")
        feedback = Feedback.model_validate(matches[0])
        self._validate_capture(record, actor)
        source = _FeedbackSource(
            experience_id=record.experience_id,
            dataset_id=dataset_id,
            dataset_hash=dataset["content_hash"],
            feedback_id=feedback.feedback_id,
            task_id=feedback.task_id,
            result_hash=feedback.result_hash,
            split="training",
        ).model_dump(mode="json")
        with closing(self._connect()) as db, db:
            self._insert_candidate(db, record, actor, "feedback")
            db.execute(
                "INSERT INTO experience_feedback_sources VALUES(?,?,?)",
                (str(record.experience_id), json.dumps(source), content_hash(source)),
            )
        return str(record.experience_id)

    def capture_from_extraction(
        self,
        candidate,
        actor: ActorContext,
        *,
        trace_bytes: bytes,
        document_bytes: bytes,
        extractor_hash: str,
    ) -> str:
        """Capture an extracted candidate bound to the exact bytes it came from.

        The template is re-run over the supplied bytes and the result must match
        the candidate exactly, so passing digests alone cannot bind an unrelated
        or edited record. Extraction provenance never substitutes for review.
        """
        self._validate_capture(candidate.record, actor)
        if not isinstance(trace_bytes, bytes) or not isinstance(document_bytes, bytes):
            raise ExperienceError("experience_extraction_source_denied")
        if not isinstance(extractor_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", extractor_hash):
            raise ExperienceError("experience_extraction_source_denied")
        from generaldataagent.evaluation.experience_extraction import extract_rule_experience

        try:
            trace = json.loads(trace_bytes.decode("utf-8"))
            documents = document_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise ExperienceError("experience_extraction_source_denied") from None
        if not isinstance(trace, list) or not all(isinstance(event, dict) for event in trace):
            raise ExperienceError("experience_extraction_source_denied")
        record = candidate.record
        produced = extract_rule_experience(
            trace, documents=documents, owner=record.owner, created_at=record.created_at
        )
        expected = {**record.model_dump(mode="json")}
        matches = [
            item
            for item in produced
            if item.template_version == candidate.template_version
            and (item.failure_step, item.recovery_step)
            == (candidate.failure_step, candidate.recovery_step)
            and item.result_hash == candidate.result_hash
            and {
                **item.record.model_dump(mode="json"),
                "experience_id": str(record.experience_id),
            }
            == expected
        ]
        if len(matches) != 1:
            raise ExperienceError("experience_extraction_source_denied")
        source = _ExtractionSource(
            experience_id=record.experience_id,
            template_version=candidate.template_version,
            failure_step=candidate.failure_step,
            recovery_step=candidate.recovery_step,
            result_hash=candidate.result_hash,
            trace_hash=hashlib.sha256(trace_bytes).hexdigest(),
            documents_hash=hashlib.sha256(document_bytes).hexdigest(),
            extractor_hash=extractor_hash,
        ).model_dump(mode="json")
        with closing(self._connect()) as db, db:
            self._insert_candidate(db, record, actor, "extraction")
            db.execute(
                "INSERT INTO experience_extraction_sources VALUES(?,?,?)",
                (str(record.experience_id), json.dumps(source), content_hash(source)),
            )
        return str(record.experience_id)

    def extraction_source(self, experience_id: UUID, actor: ActorContext) -> dict:
        """Read a validated extraction receipt; manual records have none."""
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            record = self._load(db, experience_id, actor)
            if self._origin(db, record) != "extraction":
                raise ExperienceError("experience_origin_mismatch")
            return self._extraction_receipt(db, record)

    @staticmethod
    def _extraction_receipt(db, record: ExperienceRecord) -> dict:
        """Validate the stored receipt; origin is derived, never stored twice."""
        row = db.execute(
            "SELECT body,hash FROM experience_extraction_sources WHERE id=?",
            (str(record.experience_id),),
        ).fetchone()
        if row is None:
            raise ExperienceError("experience_integrity_failure")
        try:
            source = _ExtractionSource.model_validate_json(row[0])
        except (TypeError, ValidationError) as exc:
            raise ExperienceError("experience_integrity_failure") from exc
        body = source.model_dump(mode="json")
        if (
            source.experience_id != record.experience_id
            or source.template_version != "rule-excerpt-v1"
            or source.recovery_step != source.failure_step + 1
            or content_hash(body) != row[1]
        ):
            raise ExperienceError("experience_integrity_failure")
        return {**body, "origin": "extraction"}

    def review(
        self,
        experience_id: UUID,
        *,
        reviewer_id: str,
        accepted: bool,
        actor: ActorContext,
        registry: FeedbackRegistry | None = None,
    ) -> None:
        """Independent review; the capturing user may not review their own record."""
        self._require_writable()
        if not reviewer_id.strip() or type(accepted) is not bool:
            raise ExperienceError("experience_review_denied")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            record = self._load(db, experience_id, actor)
            if reviewer_id == record.owner:
                raise ExperienceError("experience_review_denied")
            if accepted:
                source = self._feedback_source(db, record)
                if source is not None:
                    if registry is None:
                        raise ExperienceError("experience_feedback_source_denied")
                    self._verify_feedback_source(source, actor, registry)
            try:
                db.execute(
                    "INSERT INTO experience_reviews VALUES(?,?,?)",
                    (str(experience_id), reviewer_id, int(accepted)),
                )
            except sqlite3.IntegrityError as exc:
                raise ExperienceError("experience_review_denied") from exc
            # The active state is derived from every review, not just this one.
            # A single rejection withdraws the record for good; later approvals
            # cannot silently revive material an independent reviewer refused.
            verdicts = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(accepted), 0) FROM experience_reviews WHERE id=?",
                (str(experience_id),),
            ).fetchone()
            active = verdicts[0] > 0 and verdicts[1] == verdicts[0]
            db.execute(
                "UPDATE experiences SET state=? WHERE id=?",
                ("active" if active else "candidate", str(experience_id)),
            )

    @staticmethod
    def _decode_record(body: str, digest: str, experience_id: str | UUID) -> ExperienceRecord:
        try:
            record = ExperienceRecord.model_validate_json(body)
        except (TypeError, ValueError) as exc:
            raise ExperienceError("experience_integrity_failure") from exc
        if (
            str(record.experience_id) != str(experience_id)
            or content_hash(record.model_dump(mode="json")) != digest
        ):
            raise ExperienceError("experience_integrity_failure")
        return record

    @staticmethod
    def _check_review_state(db, experience_id, record: ExperienceRecord, state: str) -> None:
        reviews = db.execute(
            "SELECT reviewer,accepted FROM experience_reviews WHERE id=?", (str(experience_id),)
        ).fetchall()
        for reviewer, accepted in reviews:
            if (
                not isinstance(reviewer, str)
                or not reviewer.strip()
                or reviewer == record.owner
                or type(accepted) is not int
                or accepted not in (0, 1)
            ):
                raise ExperienceError("experience_integrity_failure")
        expected = "active" if reviews and all(row[1] == 1 for row in reviews) else "candidate"
        if state != expected:
            raise ExperienceError("experience_integrity_failure")

    def _load(self, db, experience_id: UUID, actor: ActorContext) -> ExperienceRecord:
        row = db.execute(
            "SELECT scope,body,hash,state FROM experiences WHERE id=?", (str(experience_id),)
        ).fetchone()
        if row is None or row[0] != self._scope(actor):
            raise ExperienceError("experience_scope_mismatch")
        record = self._decode_record(row[1], row[2], experience_id)
        if record.owner != actor.user_id:
            raise ExperienceError("experience_integrity_failure")
        self._check_capture_receipts(db, record)
        self._check_review_state(db, experience_id, record, row[3])
        return record

    @classmethod
    def _check_capture_receipts(cls, db, record: ExperienceRecord) -> str:
        """Validate whichever receipt the record claims, on every read path.

        A derived record whose receipt is missing, unreadable or inconsistent is
        corrupt even if its own body and review rows are intact.
        """
        origin = cls._origin(db, record)
        if origin == "feedback":
            if cls._feedback_source(db, record) is None:
                raise ExperienceError("experience_integrity_failure")
        elif origin == "extraction":
            cls._extraction_receipt(db, record)
        return origin

    @staticmethod
    def _receipt_present(db, experience_id: UUID) -> bool:
        return ExperienceStore._table_has_receipt(db, "experience_feedback_sources", experience_id)

    @staticmethod
    def _extraction_present(db, experience_id: UUID) -> bool:
        return ExperienceStore._table_has_receipt(
            db, "experience_extraction_sources", experience_id
        )

    @staticmethod
    def _table_has_receipt(db, table: str, experience_id: UUID) -> bool:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        ):
            return False
        return (
            db.execute(f"SELECT 1 FROM {table} WHERE id=?", (str(experience_id),)).fetchone()
            is not None
        )

    @classmethod
    def _origin(cls, db, record: ExperienceRecord) -> str:
        """Record provenance; a missing receipt on a derived record is corruption."""
        feedback_present = cls._receipt_present(db, record.experience_id)
        extraction_present = cls._extraction_present(db, record.experience_id)
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='experience_origins'"
            ).fetchone()
            is None
        ):
            # Snapshots taken before provenance existed derive it from receipts.
            if feedback_present and extraction_present:
                raise ExperienceError("experience_integrity_failure")
            if feedback_present:
                return "feedback"
            return "extraction" if extraction_present else "manual"
        row = db.execute(
            "SELECT origin FROM experience_origins WHERE id=?", (str(record.experience_id),)
        ).fetchone()
        if row is None:
            # A marker is written with every capture; losing it is not a manual record.
            if feedback_present and extraction_present:
                raise ExperienceError("experience_integrity_failure")
            if feedback_present:
                return "feedback"
            return "extraction" if extraction_present else "manual"
        if row[0] not in {"manual", "feedback", "extraction"}:
            raise ExperienceError("experience_integrity_failure")
        expected = (
            "feedback" if feedback_present else "extraction" if extraction_present else "manual"
        )
        if feedback_present and extraction_present or row[0] != expected:
            raise ExperienceError("experience_integrity_failure")
        return row[0]

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
        selected = []
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            cursor = db.execute(
                "SELECT id,body,hash,state FROM experiences "
                "WHERE scope=? AND state='active' ORDER BY rowid DESC",
                (scope,),
            )
            # Bound each batch, not the candidate population: newer unrelated
            # lessons must not hide an older match. Verify before filtering so
            # corrupted metadata cannot silently turn a match into a nonmatch.
            while rows := cursor.fetchmany(200):
                for experience_id, body, digest, state in rows:
                    record = self._decode_record(body, digest, experience_id)
                    if record.owner != actor.user_id:
                        raise ExperienceError("experience_integrity_failure")
                    self._check_capture_receipts(db, record)
                    self._check_review_state(db, experience_id, record, state)
                    if record.failure_class != failure_class or tool not in record.tool_scope:
                        continue
                    selected.append(record)
                    if len(selected) == limit:
                        return tuple(selected)
        return tuple(selected)

    @staticmethod
    def _feedback_source(db, record: ExperienceRecord) -> dict | None:
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='experience_feedback_sources'"
            ).fetchone()
            is None
        ):
            return None
        row = db.execute(
            "SELECT body,hash FROM experience_feedback_sources WHERE id=?",
            (str(record.experience_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            source = _FeedbackSource.model_validate_json(row[0])
        except (TypeError, ValidationError) as exc:
            raise ExperienceError("experience_integrity_failure") from exc
        body = source.model_dump(mode="json")
        if source.experience_id != record.experience_id or content_hash(body) != row[1]:
            raise ExperienceError("experience_integrity_failure")
        return body

    def feedback_source(self, experience_id: UUID, actor: ActorContext) -> dict | None:
        """Read a validated capture receipt for review; absence is not source qualification."""
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            record = self._load(db, experience_id, actor)
            return self._feedback_source(db, record)

    def verify_feedback_source(
        self, experience_id: UUID, actor: ActorContext, *, registry: FeedbackRegistry
    ) -> dict:
        """Recheck a capture receipt against frozen feedback; never activate the lesson."""
        source = self.feedback_source(experience_id, actor)
        if source is None:
            raise ExperienceError("experience_feedback_source_denied")
        self._verify_feedback_source(source, actor, registry)
        return source

    @staticmethod
    def _verify_feedback_source(
        source: dict, actor: ActorContext, registry: FeedbackRegistry
    ) -> None:
        if not isinstance(registry, FeedbackRegistry):
            raise ValueError("feedback verification requires a feedback registry")
        dataset = registry.read_dataset(UUID(source["dataset_id"]), actor)
        if (
            dataset.get("split") != "training"
            or dataset.get("content_hash") != source["dataset_hash"]
        ):
            raise ExperienceError("experience_feedback_source_denied")
        matches = [
            example
            for example in dataset["examples"]
            if example.get("feedback_id") == source["feedback_id"]
        ]
        if len(matches) != 1:
            raise ExperienceError("experience_feedback_source_denied")
        feedback = Feedback.model_validate(matches[0])
        if (
            str(feedback.task_id) != source["task_id"]
            or feedback.result_hash != source["result_hash"]
        ):
            raise ExperienceError("experience_feedback_source_denied")

    def state(self, experience_id: UUID, actor: ActorContext) -> str:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self._load(db, experience_id, actor)
            row = db.execute(
                "SELECT state FROM experiences WHERE id=?", (str(experience_id),)
            ).fetchone()
        return row[0]

    def audit(self) -> dict:
        """Read-only consistency sweep across every stored record.

        Existence checks run for all rows, including records that normal reads
        would never surface: a corrupted record must be reported rather than
        silently skipped. Only identifiers and stable codes are emitted; no
        lesson text, owner identity or scope value leaves the store.
        """
        findings = []
        by_origin: Counter = Counter()
        by_state: Counter = Counter()
        scopes = set()
        records = 0
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT id,scope,body,hash,state FROM experiences").fetchall()
            for experience_id, scope, body, digest, state in rows:
                records += 1
                scopes.add(scope)
                code = self._audit_record(db, experience_id, body, digest, state, by_origin)
                if code is None and (
                    not isinstance(scope, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", scope)
                ):
                    code = "invalid_scope"
                by_state[state if state in {"candidate", "active"} else "unknown"] += 1
                if code is not None:
                    findings.append({"experience_id": experience_id, "code": code})
            for table, code in (
                ("experience_reviews", "orphan_review"),
                ("experience_origins", "orphan_origin"),
                ("experience_feedback_sources", "orphan_feedback_receipt"),
                ("experience_extraction_sources", "orphan_extraction_receipt"),
            ):
                if (
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    is None
                ):
                    continue
                # These table names are fixed above. One finding per missing
                # parent suffices even when it had multiple reviewer rows.
                orphans = db.execute(
                    f"SELECT DISTINCT a.id FROM {table} a WHERE NOT EXISTS "
                    "(SELECT 1 FROM experiences e WHERE e.id=a.id) ORDER BY a.id"
                )
                findings.extend(
                    {"experience_id": experience_id, "code": code} for (experience_id,) in orphans
                )
        return {
            "records": records,
            "scopes": len(scopes),
            "by_origin": dict(sorted(by_origin.items())),
            "by_state": dict(sorted(by_state.items())),
            "findings": findings,
            "consistent": not findings,
        }

    @classmethod
    def _audit_record(
        cls, db, experience_id: str, body: str, digest: str, state: str, by_origin: Counter
    ) -> str | None:
        try:
            record = cls._decode_record(body, digest, experience_id)
        except ExperienceError:
            # Origin still counts when readable so operators can triage shapes.
            try:
                by_origin[cls._origin_for_id(db, experience_id)] += 1
            except ExperienceError:
                by_origin["unknown"] += 1
            return "unreadable_record"
        try:
            origin = cls._origin(db, record)
        except ExperienceError:
            by_origin["unknown"] += 1
            return "origin_mismatch"
        by_origin[origin] += 1
        if origin == "feedback":
            try:
                if cls._feedback_source(db, record) is None:
                    return "invalid_capture_receipt"
            except ExperienceError:
                return "invalid_capture_receipt"
        if origin == "extraction":
            try:
                cls._extraction_receipt(db, record)
            except ExperienceError:
                return "invalid_capture_receipt"
        try:
            cls._check_review_state(db, UUID(experience_id), record, state)
        except ExperienceError:
            return "invalid_review_evidence"
        return None

    @classmethod
    def _origin_for_id(cls, db, experience_id: str) -> str:
        """Provenance for a record too corrupt to decode; receipts decide it."""
        feedback = cls._table_has_receipt(db, "experience_feedback_sources", experience_id)
        extraction = cls._table_has_receipt(db, "experience_extraction_sources", experience_id)
        if feedback and extraction:
            raise ExperienceError("experience_integrity_failure")
        if feedback:
            return "feedback"
        return "extraction" if extraction else "manual"


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


_HINT_CHAR_BUDGET = 2000


def experience_hints(
    store: ExperienceStore,
    actor: ActorContext,
    *,
    limit: int = 3,
    char_budget: int = _HINT_CHAR_BUDGET,
):
    """Bind one actor scope to a retrieval callable an evaluator can hold.

    The agent receives only ``(failure_class, tool) -> briefs``. It never sees
    the store, the scope or the actor, so a prompt cannot widen its own access.

    Briefs are admitted whole until the character budget is spent. A record
    whose repair text cannot fit is dropped rather than truncated: half a
    repair instruction is worse than none, and the trace reports the drop.
    """
    if not isinstance(store, ExperienceStore):
        raise ValueError("experience hints require an experience store")
    if not isinstance(actor, ActorContext):
        raise ValueError("experience hints require an actor scope")
    if type(char_budget) is not int or char_budget < 1:
        raise ValueError("experience hint budget must be a positive integer")
    last_budget = {"budget": char_budget, "used": 0, "truncated": 0, "offered": 0}

    def retrieve(failure_class: str, tool: str) -> tuple[dict, ...]:
        # A failed lookup must not expose counts from the previous call.
        last_budget.update(used=0, truncated=0, offered=0)
        records = store.retrieve(failure_class=failure_class, tool=tool, actor=actor, limit=limit)
        admitted, used, truncated = [], 0, 0
        for brief in experience_brief(records):
            # Match json.dumps(feedback) in the agent, including ASCII escapes
            # and the list brackets or separator around this additional brief.
            size = len(json.dumps(brief)) + 2
            if used + size > char_budget:
                truncated += 1
                continue
            admitted.append(brief)
            used += size
        last_budget.update(used=used, truncated=truncated, offered=len(records))
        return tuple(admitted)

    retrieve.last_budget = last_budget
    return retrieve


__all__ = [
    "ExperienceError",
    "ExperienceRecord",
    "ExperienceStore",
    "experience_brief",
    "experience_hints",
]
