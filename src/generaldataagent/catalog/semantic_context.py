"""Read-only, scoped field and relationship evidence with explicit preview limits."""

import json
import sqlite3

from generaldataagent.evaluation.stage_six_sql_precision import schema_relationships
from generaldataagent.tools.sql_guard import SqlGuard
from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor


def semantic_context(database, *, allowed_relations: set[str], max_chars: int = 4000) -> dict:
    if max_chars < 1000:
        raise ValueError("semantic context budget too small")
    relationships = schema_relationships(database, allowed_relations=allowed_relations)
    executor = SQLiteReadOnlyExecutor(database, max_rows=10, timeout_seconds=5)
    guard = SqlGuard(dialect="sqlite")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    def query(sql):
        guard.inspect(sql, allowed_relations=allowed_relations)
        return executor.execute(sql).rows

    def preview(value):
        if isinstance(value, bytes):
            return {"type": "blob", "byte_count": len(value)}
        return value[:160] if isinstance(value, str) else value

    tables = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for table in sorted(allowed_relations)[:12]:
            fields = []
            columns = db.execute("PRAGMA table_info(" + quote(table) + ")").fetchall()
            for col in columns[:24]:
                name, declared_type = col[1], col[2]
                samples = query(
                    f"SELECT DISTINCT {quote(name)} FROM {quote(table)} "
                    f"WHERE {quote(name)} IS NOT NULL ORDER BY {quote(name)} LIMIT 6"
                )
                fields.append(
                    {
                        "name": name,
                        "declared_type": declared_type,
                        "enum_preview": [preview(row[0]) for row in samples[:5]],
                        "value_preview_limit_chars": 160,
                        "preview_complete": len(samples) < 6,
                        "aliases": [],
                        "date_encoding": "unverified; inspect public schema and values",
                    }
                )
            count = query(f"SELECT COUNT(*) FROM {quote(table)}")[0][0]
            tables[table] = {
                "row_count": count,
                "fields": fields,
                "omitted_fields": max(0, len(columns) - 24),
            }
    # Composite FK components must be profiled together, never as independent joins.
    groups = {}
    for fk in relationships["foreign_keys"]:
        groups.setdefault((fk["table"], fk["relationship"]), []).append(fk)
    cardinalities = []
    for (table, _), components in sorted(groups.items())[:24]:
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
    output = {
        "tables": tables,
        "omitted_table_previews": max(0, len(allowed_relations) - 12),
        "omitted_cardinality_checks": max(0, len(groups) - 24),
        "relationships": relationships,
        "cardinalities": cardinalities,
        "scope": "untrusted observations; aliases and date semantics require public evidence",
    }

    # Field schemas already appear in database_context: retain only bounded extra observations.
    for table in output["tables"].values():
        for field in table["fields"]:
            field.pop("aliases", None)
            field.pop("date_encoding", None)
            field.pop("declared_type", None)
    output["budget_chars"] = max_chars
    while len(json.dumps(output, default=str)) > max_chars:
        fields = [t for t in output["tables"].values() if t["fields"]]
        if fields:
            table = max(fields, key=lambda t: len(json.dumps(t)))
            table["fields"].pop()
            table["omitted_fields"] += 1
        elif output["cardinalities"]:
            output["cardinalities"].pop()
            output["omitted_cardinality_checks"] += 1
        elif output["relationships"]["foreign_keys"]:
            output["relationships"]["foreign_keys"].pop()
            output["omitted_declared_relationships"] = (
                output.get("omitted_declared_relationships", 0) + 1
            )
        elif output["relationships"]["primary_keys"]:
            output["relationships"]["primary_keys"].popitem()
            output["omitted_declared_relationships"] = (
                output.get("omitted_declared_relationships", 0) + 1
            )
        elif output["tables"]:
            output["tables"].popitem()
            output["omitted_table_previews"] += 1
        else:
            raise ValueError("semantic context metadata exceeds budget")
    return output
