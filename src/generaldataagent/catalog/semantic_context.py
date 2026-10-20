"""Read-only, scoped field and relationship evidence with explicit preview limits."""

import sqlite3

from generaldataagent.evaluation.stage_six_sql_precision import schema_relationships
from generaldataagent.tools.sql_guard import SqlGuard
from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor


def semantic_context(database, *, allowed_relations: set[str]) -> dict:
    relationships = schema_relationships(database, allowed_relations=allowed_relations)
    executor = SQLiteReadOnlyExecutor(database, max_rows=10, timeout_seconds=5)
    guard = SqlGuard(dialect="sqlite")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    def query(sql):
        guard.inspect(sql, allowed_relations=allowed_relations)
        return executor.execute(sql).rows

    tables = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for table in sorted(allowed_relations):
            fields = []
            for col in db.execute("PRAGMA table_info(" + quote(table) + ")"):
                name, declared_type = col[1], col[2]
                samples = query(
                    f"SELECT DISTINCT {quote(name)} FROM {quote(table)} "
                    f"WHERE {quote(name)} IS NOT NULL ORDER BY {quote(name)} LIMIT 6"
                )
                fields.append(
                    {
                        "name": name,
                        "declared_type": declared_type,
                        "enum_preview": [row[0] for row in samples[:5]],
                        "preview_complete": len(samples) < 6,
                        "aliases": [],
                        "date_encoding": "unverified; inspect public schema and values",
                    }
                )
            count = query(f"SELECT COUNT(*) FROM {quote(table)}")[0][0]
            tables[table] = {"row_count": count, "fields": fields}
    # Composite FK components must be profiled together, never as independent joins.
    groups = {}
    for fk in relationships["foreign_keys"]:
        groups.setdefault((fk["table"], fk["relationship"]), []).append(fk)
    cardinalities = []
    for (table, _), components in sorted(groups.items()):
        components.sort(key=lambda f: f["sequence"])
        target = components[0]["references_table"]
        target_pk = relationships["primary_keys"].get(target, [])
        referenced = [f["references_column"] for f in components]
        if any(c is None for c in referenced):
            if len(target_pk) != len(components):
                continue
            referenced = target_pk
        keys = ",".join(quote(f["column"]) for f in components)
        parent_keys = ",".join(quote(c) for c in referenced)
        maxima = []
        for relation, columns in ((table, keys), (target, parent_keys)):
            rows = query(
                f"SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM {quote(relation)} "
                f"GROUP BY {columns})"
            )
            maxima.append(rows[0][0] or 0)
        cardinalities.append(
            {
                "table": table,
                "references_table": target,
                "columns": [f["column"] for f in components],
                "references_columns": referenced,
                "max_rows_per_key": maxima,
                "many_to_many_risk": all(n > 1 for n in maxima),
                "scope": "observed key multiplicities including null groups; not join row count",
            }
        )
    return {
        "tables": tables,
        "relationships": relationships,
        "cardinalities": cardinalities,
        "scope": "untrusted observations; aliases and date semantics require public evidence",
    }
