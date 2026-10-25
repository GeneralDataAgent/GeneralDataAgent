"""Declared SQLite keys and relationships, independent of any evaluation profile.

Read-only introspection of constraints the schema declares. Absence of a
constraint is recorded as absence, never inferred into a business
relationship, and rows are never read.
"""

import sqlite3


def schema_relationships(database, *, allowed_relations: set[str]) -> dict:
    """Expose declared keys, not data-derived guesses or reference queries."""

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    primary_keys, foreign_keys = {}, []
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        physical = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not allowed_relations <= physical:
            raise ValueError("unknown schema relation")
        ascii_case = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        canonical = {name.translate(ascii_case): name for name in allowed_relations}
        for name in sorted(allowed_relations):
            columns = db.execute("PRAGMA table_info(" + quote(name) + ")").fetchall()
            keys = sorted((row[5], row[1]) for row in columns if row[5])
            if keys:
                primary_keys[name] = [column for _, column in keys]
            for row in db.execute("PRAGMA foreign_key_list(" + quote(name) + ")"):
                target = canonical.get(row[2].translate(ascii_case))
                if target is not None:
                    foreign_keys.append(
                        {
                            "table": name,
                            "relationship": row[0],
                            "sequence": row[1],
                            "column": row[3],
                            "references_table": target,
                            "references_column": row[4],
                        }
                    )
    return {
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
        "scope": "declared SQLite constraints only; absence is not proof of no relationship",
    }
