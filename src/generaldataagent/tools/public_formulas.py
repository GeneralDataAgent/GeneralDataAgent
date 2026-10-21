"""Narrow checks of explicit public formula fields against physical SQL sources."""

import re

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from generaldataagent.tools.semantic_requirements import _expression, _identifier_key, _unparen


def check_public_year_fields(sql: str, question: str, instructions: str, schema: dict) -> list[str]:
    """Check a uniquely sourced age year-difference; unknown lineage stays unclassified.

    The schema must be the runtime's authorized physical schema, not a model proposal.
    This does not certify date encodings, selected populations or business interpretation.
    """
    if not re.search(r"\bage\b|\bhow old\b", question, re.I):
        return []
    public = "\n".join(
        clause
        for clause in re.split(r"[;\n]", instructions)
        if not re.search(r"\b(?:do not|don't|never|must not|not to)\b", clause, re.I)
    )
    year = r"\byear\s*\(([^()]{1,200})\)(?:\s*\))*"
    pattern = year + r"\s*-\s*(?:\(\s*)*" + year
    pattern += r"|\bsubtract\s*\(\s*(?:\(\s*)*" + year + r"\s*,\s*(?:\(\s*)*" + year
    matches = list(re.finditer(pattern, public, re.I))
    if len(matches) != 1:
        return []
    args = [value for value in matches[0].groups() if value is not None]

    def public_field(text):
        try:
            column = _expression(text.strip())
        except (ValueError, TypeError, SqlglotError):
            return None
        if not isinstance(column, exp.Column) or column.db or column.catalog:
            return None
        candidates = [
            (_identifier_key(table), _identifier_key(name))
            for table, names in schema.items()
            for name in names
            if _identifier_key(column.name) == _identifier_key(name)
            and (not column.table or _identifier_key(column.table) == _identifier_key(table))
        ]
        return candidates[0] if len(candidates) == 1 else None

    expected = [public_field(arg) for arg in args]
    if len(expected) != 2 or any(value is None for value in expected):
        return []
    tree = parse_one(sql, read="sqlite")
    if not isinstance(tree, exp.Select):
        return []
    scope = next((scope for scope in traverse_scope(tree) if scope.expression is tree), None)
    if scope is None:
        return []

    def physical_field(column):
        if column.find_ancestor(exp.Select) is not tree:
            return None
        candidates = []
        for alias, (_, source) in scope.selected_sources.items():
            if not isinstance(source, exp.Table) or source.db or source.catalog:
                continue
            if column.table and _identifier_key(column.table) != _identifier_key(alias):
                continue
            for table, names in schema.items():
                if _identifier_key(source.name) == _identifier_key(table):
                    candidates.extend(
                        (_identifier_key(table), _identifier_key(name))
                        for name in names
                        if _identifier_key(column.name) == _identifier_key(name)
                    )
        return candidates[0] if len(candidates) == 1 else None

    def year_field(node):
        node = _unparen(node)
        while isinstance(node, (exp.Cast, exp.Round)):
            node = _unparen(node.this)
        if isinstance(node, exp.TimeToStr):
            fmt = node.args.get("format")
            if not isinstance(fmt, exp.Literal) or fmt.this != "%Y":
                return None
        elif not isinstance(node, exp.Year):
            return None
        columns = list(node.find_all(exp.Column))
        return physical_field(columns[0]) if len(columns) == 1 else None

    for projection in tree.selects:
        # Additional outputs can contain unrelated year differences. An age output
        # must be identifiable, or be the sole projection, before applying this rule.
        if len(tree.selects) != 1 and not re.search(
            r"(?:^|[\s_])age(?:$|[\s_])", projection.alias_or_name, re.I
        ):
            continue
        node = projection.this if isinstance(projection, exp.Alias) else projection
        node = _unparen(node)
        while isinstance(node, (exp.Cast, exp.Round)):
            node = _unparen(node.this)
        if not isinstance(node, exp.Sub):
            continue
        actual = [year_field(node.this), year_field(node.expression)]
        if all(value is not None for value in actual) and actual != expected:
            return ["public_year_formula_fields"]
    return []
