"""Bounded observations for name-based joins, never inferred business relationships."""

import re

from generaldataagent.tools.sql_guard import SqlGuard
from generaldataagent.tools.sqlite import QueryExecutionError, SQLiteReadOnlyExecutor


def observed_join_candidates(database, *, schema, relationships, mappings, public_text):
    """Probe at most two scalar declared-key candidates in the supplied scope.

    A nonzero literal observation selects candidate parents, not a business interpretation.
    Naming merely proposes a join; full-table match counts neither declare an FK nor prove it.
    Parent keys come from scalar primary keys or scalar declared FK targets. Neither
    those declarations nor matching names certify uniqueness under the join's collation.
    Composite or entirely undeclared parent keys are deliberately not inferred.
    """
    ascii_case = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

    def key(name):
        return name.translate(ascii_case).replace("_", "")

    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    targets = {
        observation["candidate"][0]
        for mapping in mappings.get("ambiguous", [])
        for observation in mapping.get("literal_observations", [])
        if observation.get("exact_match_rows", 0) > 0 and observation["candidate"][0] in schema
    }
    declared = {
        (fk["table"], fk["column"], fk["references_table"], fk["references_column"])
        for fk in relationships["foreign_keys"]
    }
    target_keys = {
        table: set(keys) if len(keys) == 1 else set()
        for table, keys in relationships["primary_keys"].items()
    }
    foreign_key_groups = {}
    for fk in relationships["foreign_keys"]:
        foreign_key_groups.setdefault((fk["table"], fk["relationship"]), []).append(fk)
    for components in foreign_key_groups.values():
        if len(components) == 1 and components[0]["references_column"] is not None:
            fk = components[0]
            target_keys.setdefault(fk["references_table"], set()).add(fk["references_column"])
    candidates = set()
    for parent in targets:
        stem = key(parent)
        stems = {stem, stem[:-1]} if stem.endswith("s") else {stem}
        for parent_key in target_keys.get(parent, set()):
            if parent_key not in schema[parent]:
                continue
            expected = {prefix + key(parent_key) for prefix in stems}
            for child, fields in schema.items():
                if child == parent:
                    continue
                for field in fields:
                    candidate = (child, field, parent, parent_key)
                    if key(field) in expected and candidate not in declared:
                        candidates.add(candidate)
    candidates = sorted(
        candidates,
        key=lambda item: (
            not bool(re.search(r"(?<!\w)" + re.escape(item[0]) + r"(?!\w)", public_text, re.I)),
            item,
        ),
    )
    guard = SqlGuard(dialect="sqlite")
    executor = SQLiteReadOnlyExecutor(database, max_rows=1, timeout_seconds=1)
    records, failures = [], 0
    for child, field, parent, parent_key in candidates[:2]:
        sql = (
            f"SELECT (SELECT COUNT({quote(field)}) FROM {quote(child)}),"
            f"(SELECT COUNT(*) FROM {quote(child)} x WHERE x.{quote(field)} IS NOT NULL "
            f"AND EXISTS(SELECT 1 FROM {quote(parent)} y "
            f"WHERE x.{quote(field)}=y.{quote(parent_key)})),COUNT(p.{quote(parent_key)}) "
            f"FROM {quote(child)} c LEFT JOIN {quote(parent)} p "
            f"ON c.{quote(field)}=p.{quote(parent_key)}"
        )
        guard.inspect(sql, allowed_relations=set(schema))
        record = {"keys": [child, field, parent, parent_key]}
        try:
            non_null, matched, joined = executor.execute(sql).rows[0]
        except QueryExecutionError:
            failures += 1
            record["status"] = "unavailable"
        else:
            record.update(
                non_null_child_rows=non_null,
                matched_child_rows=matched,
                matched_join_rows=joined,
                join_expands_matches=joined > matched,
            )
        records.append(record)
    return {"candidates": records, "failures": failures, "omitted": max(0, len(candidates) - 2)}
