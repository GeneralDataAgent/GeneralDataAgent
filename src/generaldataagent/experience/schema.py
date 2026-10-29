"""Explicit SQLite layouts for experience stores; normal opening never migrates."""

from __future__ import annotations

import sqlite3
from contextlib import closing

VERSION = 1
TABLES = {
    "experiences": "id TEXT PRIMARY KEY, scope TEXT, body TEXT, hash TEXT, state TEXT",
    "experience_reviews": "id TEXT, reviewer TEXT, accepted INTEGER, PRIMARY KEY(id,reviewer)",
    "experience_feedback_sources": "id TEXT PRIMARY KEY, body TEXT, hash TEXT",
    "experience_origins": "id TEXT PRIMARY KEY, origin TEXT",
    "experience_extraction_sources": "id TEXT PRIMARY KEY, body TEXT, hash TEXT",
    "experience_schema": "version INTEGER PRIMARY KEY, source_layout TEXT NOT NULL",
}
_BASE = {"experiences", "experience_reviews"}
LEGACY_LAYOUTS = {
    "manual-v0": _BASE,
    "feedback-v0": _BASE | {"experience_feedback_sources"},
    "receipts-v0": _BASE | {"experience_feedback_sources", "experience_extraction_sources"},
    "origins-v0": set(TABLES) - {"experience_schema", "experience_extraction_sources"},
    "complete-v0": set(TABLES) - {"experience_schema"},
}


class ExperienceSchemaError(ValueError):
    """Stable schema refusal without database contents or paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def table_names(db: sqlite3.Connection) -> set[str]:
    return {
        name
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def require_layout(db: sqlite3.Connection, names: set[str]) -> None:
    if (
        table_names(db) != names
        or db.execute("SELECT 1 FROM sqlite_master WHERE type='view' LIMIT 1").fetchone()
    ):
        raise ExperienceSchemaError("experience_schema_invalid")
    # Compare SQLite's structural description, not SQL spelling. Fixed table
    # declarations supply all identifiers, including for legacy layouts.
    with closing(sqlite3.connect(":memory:")) as expected:
        for name in sorted(names):
            expected.execute(f"CREATE TABLE {name} ({TABLES[name]})")
            if (
                db.execute(f"PRAGMA table_xinfo({name})").fetchall()
                != expected.execute(f"PRAGMA table_xinfo({name})").fetchall()
            ):
                raise ExperienceSchemaError("experience_schema_invalid")


def validate_schema(db: sqlite3.Connection) -> None:
    if "experience_schema" not in table_names(db):
        raise ExperienceSchemaError("experience_migration_required")
    require_layout(db, set(TABLES))
    rows = db.execute("SELECT version,source_layout FROM experience_schema").fetchall()
    if len(rows) != 1:
        raise ExperienceSchemaError("experience_schema_invalid")
    version, source = rows[0]
    if version != VERSION:
        raise ExperienceSchemaError("experience_schema_unsupported")
    if source not in {"new-v1", *LEGACY_LAYOUTS}:
        raise ExperienceSchemaError("experience_schema_invalid")


def initialize_schema(db: sqlite3.Connection) -> None:
    """Create a new store atomically, or validate an existing one unchanged."""
    db.execute("BEGIN IMMEDIATE")
    if (
        table_names(db)
        or db.execute("SELECT 1 FROM sqlite_master WHERE type='view' LIMIT 1").fetchone()
    ):
        validate_schema(db)
        return
    for name, columns in TABLES.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
    db.execute("INSERT INTO experience_schema VALUES(?,?)", (VERSION, "new-v1"))
