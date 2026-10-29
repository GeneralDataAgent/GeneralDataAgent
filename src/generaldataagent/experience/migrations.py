"""Copy an explicitly declared legacy layout into a new versioned store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from generaldataagent.experience.schema import (
    LEGACY_LAYOUTS,
    TABLES,
    VERSION,
    ExperienceSchemaError,
    require_layout,
    table_names,
)


def migrate_legacy_store(source: Path, output: Path, *, legacy_layout: str) -> dict:
    """Preserve the source, including row bytes and reviews; never activate lessons.

    The operator's layout declaration is checked, not inferred from absent
    tables. Pre-marker layouts can only recover provenance from surviving
    receipts; their authenticity and any prior deletion remain unknowable.
    """
    if legacy_layout not in LEGACY_LAYOUTS:
        raise ExperienceSchemaError("experience_legacy_layout_unknown")
    if output.exists() or output.is_symlink():
        raise FileExistsError("experience_migration_output_exists")
    # SQLite backup includes committed WAL contents in one consistent snapshot.
    # Its digest identifies that snapshot, not necessarily the source file bytes.
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as origin,
        closing(sqlite3.connect(":memory:")) as snapshot,
    ):
        origin.backup(snapshot)
        require_layout(snapshot, LEGACY_LAYOUTS[legacy_layout])
        if snapshot.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ExperienceSchemaError("experience_schema_invalid")
        source_hash = hashlib.sha256(snapshot.serialize()).hexdigest()
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".experience-migration-", dir=output.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as target:
                snapshot.backup(target)
                with target:
                    target.execute("BEGIN IMMEDIATE")
                    existing = table_names(target)
                    for table, columns in TABLES.items():
                        if table not in existing:
                            target.execute(f"CREATE TABLE {table} ({columns})")
                    if "experience_origins" not in existing:
                        target.execute(
                            "INSERT INTO experience_origins SELECT id, CASE"
                            " WHEN EXISTS(SELECT 1 FROM experience_feedback_sources s"
                            " WHERE s.id=experiences.id) THEN 'feedback'"
                            " WHEN EXISTS(SELECT 1 FROM experience_extraction_sources s"
                            " WHERE s.id=experiences.id) THEN 'extraction' ELSE 'manual' END"
                            " FROM experiences"
                        )
                    target.execute(
                        "INSERT INTO experience_schema VALUES(?,?)", (VERSION, legacy_layout)
                    )
            # Use the same record, receipt, review and orphan checks as readers.
            from generaldataagent.business.experience import ExperienceStore

            audit = ExperienceStore(temporary, clock=lambda: None, read_only=True).audit()
            if not audit["consistent"]:
                raise ExperienceSchemaError("experience_migration_integrity_failure")
            report = {
                "schema_version": VERSION,
                "source_layout": legacy_layout,
                "source_snapshot_hash": source_hash,
                "output_hash": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                "records": audit["records"],
                "by_origin": audit["by_origin"],
                "by_state": audit["by_state"],
                "source_authenticated": False,
            }
            # Atomic no-clobber publication, including a concurrent publisher.
            os.link(temporary, output)
            return report
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New database path")
    parser.add_argument("--legacy-layout", choices=tuple(LEGACY_LAYOUTS), required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            migrate_legacy_store(args.source, args.output, legacy_layout=args.legacy_layout),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
