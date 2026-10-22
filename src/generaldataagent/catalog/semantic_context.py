"""Read-only, scoped field and relationship evidence with explicit preview limits."""

import json
import re
import sqlite3

from generaldataagent.catalog.field_evidence import (
    date_candidates,
    enum_label_candidates,
    public_field_mappings,
    public_name_mentioned,
    public_quoted_labels,
)
from generaldataagent.catalog.join_evidence import observed_join_candidates
from generaldataagent.evaluation.stage_six_sql_precision import schema_relationships
from generaldataagent.tools.sql_guard import SqlGuard
from generaldataagent.tools.sqlite import QueryExecutionError, SQLiteReadOnlyExecutor


def semantic_context(
    database, *, allowed_relations: set[str], max_chars: int = 4000, public_text: str = ""
) -> dict:
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

    def explicitly_mentioned(name):
        return public_name_mentioned(name, public_text)

    tables = {}
    full_schema = {}
    value_candidates = []
    enum_checks = 0
    enum_failures = 0
    profile_columns = []
    has_public_labels = bool(public_quoted_labels(public_text))
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        physical_columns = {
            table: db.execute("PRAGMA table_info(" + quote(table) + ")").fetchall()
            for table in sorted(allowed_relations)
        }
        full_schema = {table: [col[1] for col in cols] for table, cols in physical_columns.items()}
        ranked_tables = sorted(
            allowed_relations,
            key=lambda table: (
                not explicitly_mentioned(table),
                not any(explicitly_mentioned(name) for name in full_schema[table]),
                table,
            ),
        )
        for table in ranked_tables[:12]:
            fields = []
            columns = physical_columns[table]
            # Preserve physical order for ties; public mentions only prioritize
            # observations, and never certify a field's business interpretation.
            ranked_columns = sorted(columns, key=lambda col: not explicitly_mentioned(col[1]))
            for col in ranked_columns[:24]:
                name, declared_type = col[1], col[2]
                samples = query(
                    f"SELECT DISTINCT {quote(name)} FROM {quote(table)} "
                    f"WHERE {quote(name)} IS NOT NULL ORDER BY {quote(name)} LIMIT 6"
                )
                if any(isinstance(row[0], str) for row in samples):
                    profile_columns.append((table, name))
                profile = date_candidates(
                    name, [row[0] for row in samples[:5]], complete=len(samples) < 6
                )
                if (
                    enum_checks < 3
                    and has_public_labels
                    and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", public_text, re.I)
                    and any(isinstance(row[0], str) for row in samples)
                ):
                    enum_checks += 1
                    enum_sql = (
                        f"SELECT DISTINCT {quote(name)} FROM {quote(table)} "
                        f"WHERE {quote(name)} IS NOT NULL ORDER BY {quote(name)} LIMIT 65"
                    )
                    guard.inspect(enum_sql, allowed_relations=allowed_relations)
                    try:
                        domain = (
                            SQLiteReadOnlyExecutor(database, max_rows=65, timeout_seconds=2)
                            .execute(enum_sql)
                            .rows
                        )
                    except QueryExecutionError:
                        enum_failures += 1
                    else:
                        value_candidates.extend(
                            enum_label_candidates(
                                public_text,
                                table,
                                name,
                                [row[0] for row in domain],
                                complete=len(domain) < 65,
                            )
                        )
                fields.append(
                    {
                        "name": name,
                        "declared_type": declared_type,
                        "enum_preview": [preview(row[0]) for row in samples[:5]],
                        "value_preview_limit_chars": 160,
                        "preview_complete": len(samples) < 6,
                        "aliases": [],
                        "date_encoding": profile,
                    }
                )
            count = query(f"SELECT COUNT(*) FROM {quote(table)}")[0][0]
            tables[table] = {
                "row_count": count,
                "fields": fields,
                "omitted_fields": max(0, len(columns) - 24),
            }
    # Inspect at most three scoped text fields for one original public literal.
    # Literal occurrence does not establish category membership or authorize a filter.
    literal_profiles, profile_checks, profile_failures = [], 0, 0
    question_text = public_text.split("\n", 1)[0]
    labels = sorted(
        public_quoted_labels(public_text),
        key=lambda label: (
            not re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", question_text, re.I),
            label,
        ),
    )
    if labels:
        label = labels[0]
        for table, column in profile_columns[:3]:
            profile_checks += 1
            sql = (
                "SELECT COALESCE(SUM(CASE WHEN "
                + quote(column)
                + " COLLATE BINARY = ? THEN 1 ELSE 0 END),0),"
                + "COALESCE(SUM(CASE WHEN instr("
                + quote(column)
                + ",?)>0 THEN 1 ELSE 0 END),0) FROM "
                + quote(table)
            )
            guard.inspect(sql, allowed_relations=allowed_relations)
            try:
                rows = (
                    SQLiteReadOnlyExecutor(database, max_rows=1, timeout_seconds=1)
                    .execute(sql, (label, label))
                    .rows
                )
            except QueryExecutionError:
                profile_failures += 1
            else:
                exact, contains = rows[0]
                if contains:
                    literal_profiles.append(
                        {
                            "table": table,
                            "column": column,
                            "public_literal": label,
                            "exact_rows": exact,
                            "literal_contains_rows": contains,
                            "scope": (
                                "full-table binary exact/literal case-sensitive substring counts"
                            ),
                            "verified_business_semantics": False,
                        }
                    )
    mappings = public_field_mappings(public_text, full_schema)
    literal_checks = literal_failures = 0
    for mapping in [m for m in mappings["ambiguous"] if m.get("literal_kind") == "string"][:2]:
        value = mapping.get("enum_value")
        if value is None:
            continue
        observations = []
        for relation, column in mapping["candidates"][:4]:
            literal_checks += 1
            sql = f"SELECT COUNT(*) FROM {quote(relation)} WHERE {quote(column)} = ?"
            guard.inspect(sql, allowed_relations=allowed_relations)
            try:
                rows = (
                    SQLiteReadOnlyExecutor(database, max_rows=1, timeout_seconds=1)
                    .execute(sql, (value,))
                    .rows
                )
            except QueryExecutionError:
                literal_failures += 1
                observations.append({"candidate": [relation, column], "status": "unavailable"})
            else:
                observations.append(
                    {"candidate": [relation, column], "exact_match_rows": rows[0][0]}
                )
        mapping["omitted_literal_candidates"] = max(
            0, len(mapping["candidates"]) - len(observations)
        )
        mapping["literal_observations"] = observations
        mapping["observation_scope"] = "full-table exact public literal; not business certification"
    observed_joins = observed_join_candidates(
        database,
        schema=full_schema,
        relationships=relationships,
        mappings=mappings,
        public_text=public_text,
    )
    # Composite FK components must be profiled together, never as independent joins.
    groups = {}
    for fk in relationships["foreign_keys"]:
        groups.setdefault((fk["table"], fk["relationship"]), []).append(fk)
    cardinalities = []
    compact_joins = []
    for (table, _), components in sorted(groups.items())[:24]:
        components.sort(key=lambda f: f["sequence"])
        target = components[0]["references_table"]
        target_pk = relationships["primary_keys"].get(target, [])
        referenced = [f["references_column"] for f in components]
        if any(c is None for c in referenced):
            if len(target_pk) != len(components):
                continue
            referenced = target_pk
        compact_joins.append([table, [f["column"] for f in components], target, referenced])
        keys = ",".join(quote(f["column"]) for f in components)
        parent_keys = ",".join(quote(c) for c in referenced)
        maxima = []
        for relation, columns in ((table, keys), (target, parent_keys)):
            rows = query(
                f"SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM {quote(relation)} "
                f"GROUP BY {columns})"
            )
            maxima.append(rows[0][0] or 0)
        child_columns = [f["column"] for f in components]
        selections = [
            ",".join(f"{quote(c)} AS k{i}" for i, c in enumerate(columns))
            for columns in (child_columns, referenced)
        ]
        predicates = [
            " AND ".join(f"{quote(c)} IS NOT NULL" for c in columns)
            for columns in (child_columns, referenced)
        ]
        on = " AND ".join(f"c.k{i}=p.k{i}" for i in range(len(components)))
        join_sql = (
            "SELECT COALESCE(SUM(c.n*p.n),0),COALESCE(SUM(c.n),0),"
            "COALESCE(MAX(CASE WHEN c.n>1 AND p.n>1 THEN 1 ELSE 0 END),0) "
            f"FROM (SELECT {selections[0]},COUNT(*) AS n FROM {quote(table)} "
            f"WHERE {predicates[0]} GROUP BY {keys}) c "
            f"JOIN (SELECT {selections[1]},COUNT(*) AS n FROM {quote(target)} "
            f"WHERE {predicates[1]} GROUP BY {parent_keys}) p ON {on}"
        )
        join_rows, matched_left, many_to_many = query(join_sql)[0]
        cardinalities.append(
            {
                "table": table,
                "references_table": target,
                "columns": [f["column"] for f in components],
                "references_columns": referenced,
                "max_rows_per_key": maxima,
                "many_to_many_risk": bool(many_to_many),
                "inner_join_rows": join_rows,
                "matched_left_rows": matched_left,
                "expansion_per_matched_left": join_rows / matched_left if matched_left else None,
                "scope": "read-only observed inner join; null keys excluded from matching",
            }
        )
    output = {
        "tables": tables,
        "public_field_mappings": mappings,
        "observed_value_candidates": value_candidates,
        "public_literal_checks": literal_checks,
        "public_literal_failures": literal_failures,
        "enum_domain_checks": enum_checks,
        "enum_domain_failures": enum_failures,
        "omitted_table_previews": max(0, len(allowed_relations) - 12),
        "omitted_cardinality_checks": max(0, len(groups) - 24),
        "relationships": relationships,
        "cardinalities": cardinalities,
        "scope": "untrusted observations; aliases and date semantics require public evidence",
    }
    if profile_checks:
        output.update(
            public_literal_profiles=literal_profiles,
            literal_profile_checks=profile_checks,
            literal_profile_failures=profile_failures,
        )
    if observed_joins["candidates"]:
        output["observed_join_candidates"] = observed_joins["candidates"]
        output["scope"] += "; observed join candidates are not declared business relationships"
    if observed_joins["failures"]:
        output["join_candidate_failures"] = observed_joins["failures"]
    if observed_joins["omitted"]:
        output["omitted_observed_join_candidates"] = observed_joins["omitted"]

    # Field schemas already appear in database_context: retain only bounded extra observations.
    for table in output["tables"].values():
        for field in table["fields"]:
            field.pop("aliases", None)
            if field["date_encoding"] is None:
                field.pop("date_encoding")
            field.pop("declared_type", None)
    output["budget_chars"] = max_chars
    mentioned = {table for table in allowed_relations if public_name_mentioned(table, public_text)}
    literal_targets = {
        item["candidate"][0]
        for mapping in mappings["ambiguous"]
        for item in mapping.get("literal_observations", [])
        if item.get("exact_match_rows", 0) > 0
    }
    compact_joins.sort(
        key=lambda item: (
            -sum(table in literal_targets for table in (item[0], item[2])),
            -sum(table in mentioned for table in (item[0], item[2])),
            str(item),
        )
    )
    compacted = False
    while len(json.dumps(output, default=str)) > max_chars:
        fields = [t for t in output["tables"].values() if t["fields"]]
        if fields:
            candidates = [
                (table, index) for table in fields for index in range(len(table["fields"]))
            ]
            table, index = max(
                candidates,
                key=lambda item: (
                    not explicitly_mentioned(item[0]["fields"][item[1]]["name"]),
                    len(json.dumps(item[0])),
                    item[1],
                ),
            )
            table["fields"].pop(index)
            table["omitted_fields"] += 1
        elif output["tables"]:
            # Empty previews repeat schema metadata. Retain declared join keys and
            # scoped public observations before these low-information row counters.
            output["tables"].popitem()
            output["omitted_table_previews"] += 1
        elif output.get("public_literal_profiles"):
            output["public_literal_profiles"].pop()
            output["omitted_literal_profiles"] = output.get("omitted_literal_profiles", 0) + 1
        elif output["cardinalities"]:
            output["cardinalities"].pop()
            output["omitted_cardinality_checks"] += 1
        elif output["relationships"]["foreign_keys"]:
            if not compacted:
                # Preserve composite order in a compact declared representation:
                # [child relation, child columns, parent relation, parent columns].
                output["relationships"]["join_keys"] = compact_joins.copy()
                if len(groups) > len(compact_joins):
                    output["omitted_compact_join_keys"] = len(groups) - len(compact_joins)
                compacted = True
            output["relationships"]["foreign_keys"].pop()
            output["omitted_declared_relationships"] = (
                output.get("omitted_declared_relationships", 0) + 1
            )
        elif output["relationships"]["primary_keys"]:
            output["relationships"]["primary_keys"].popitem()
            output["omitted_declared_relationships"] = (
                output.get("omitted_declared_relationships", 0) + 1
            )
        elif output["relationships"].get("join_keys"):
            output["relationships"]["join_keys"].pop()
            output["omitted_compact_join_keys"] = output.get("omitted_compact_join_keys", 0) + 1
        elif output["observed_value_candidates"]:
            output["observed_value_candidates"].pop()
            output["omitted_value_candidates"] = output.get("omitted_value_candidates", 0) + 1
        elif output.get("observed_join_candidates"):
            output["observed_join_candidates"].pop()
            output["omitted_observed_join_candidates"] = (
                output.get("omitted_observed_join_candidates", 0) + 1
            )
        elif mappings["ambiguous"]:
            mappings["ambiguous"].pop()
            output["omitted_public_mappings"] = output.get("omitted_public_mappings", 0) + 1
        elif mappings["mappings"]:
            mappings["mappings"].pop()
            output["omitted_public_mappings"] = output.get("omitted_public_mappings", 0) + 1
        else:
            raise ValueError("semantic context metadata exceeds budget")
    return output
