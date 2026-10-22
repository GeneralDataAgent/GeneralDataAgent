"""Narrow checks of explicit public formula fields against physical SQL sources."""

import re

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from generaldataagent.tools.semantic_requirements import _expression, _identifier_key, _unparen


def check_public_time_pattern(
    sql: str, question: str, instructions: str, schema: dict
) -> list[str]:
    """Check only an explicit zero-hour 0:0M:SS -> M:SS% public LIKE mapping."""
    clauses = [
        clause
        for clause in re.split(r"[;\n]", instructions)
        if "0:0M:SS" in clause and not re.search(r"\bnot\b|\bnever\b", clause, re.I)
    ]
    mappings = [
        match
        for clause in clauses
        for match in re.finditer(r"\brefers to\s+(.{1,100}?)\s+LIKE\s+(['\"])M:SS%\2", clause, re.I)
    ]
    times = re.findall(r"(?<![\d:])0:(\d{2}):(\d{2})(?![\d:])", question)
    if len(mappings) != 1 or len(times) != 1:
        return []
    minute, second = map(int, times[0])
    if minute > 59 or second > 59:
        return []
    try:
        column = _expression(mappings[0].group(1).strip())
    except (ValueError, TypeError, SqlglotError):
        return []
    if not isinstance(column, exp.Column) or column.db or column.catalog:
        return []
    expected_fields = [
        (_identifier_key(table), _identifier_key(name))
        for table, names in schema.items()
        for name in names
        if _identifier_key(name) == _identifier_key(column.name)
        and (not column.table or _identifier_key(table) == _identifier_key(column.table))
    ]
    if len(expected_fields) != 1:
        return []
    tree = parse_one(sql, read="sqlite")
    if not isinstance(tree, exp.Select):
        return []
    scope = next((s for s in traverse_scope(tree) if s.expression is tree), None)
    if scope is None:
        return []
    expected = f"{minute}:{second:02d}%"
    for predicate in tree.find_all(exp.Like):
        actual, pattern = predicate.this, predicate.expression
        if predicate.find_ancestor(exp.Select) is not tree or not isinstance(actual, exp.Column):
            continue
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            continue
        fields = []
        unknown = False
        for alias, (_, source) in scope.selected_sources.items():
            if actual.table and _identifier_key(actual.table) != _identifier_key(alias):
                continue
            if not isinstance(source, exp.Table) or source.db or source.catalog:
                unknown = True
                continue
            fields.extend(
                (_identifier_key(table), _identifier_key(name))
                for table, names in schema.items()
                for name in names
                if _identifier_key(source.name) == _identifier_key(table)
                and _identifier_key(actual.name) == _identifier_key(name)
            )
        if not unknown and fields == expected_fields and pattern.this != expected:
            return ["public_time_like_pattern"]
    return []


def check_public_extremum_representation(
    sql: str, instructions: str, schema: dict, *, question: str = ""
) -> list[str]:
    """Honor a single explicit native MIN/MAX formula; abstain on unknown lineage.

    This narrow syntactic check does not prove the intended population or numeric ordering.
    An explicit conversion request, ambiguous source, or derived relation stays unclassified.
    """
    if re.search(
        r"\b(?:cast|convert|conversion|not|never|without)\b", question + "\n" + instructions, re.I
    ):
        return []
    formulas = re.findall(
        r"\b(?:refers to|means)\s+((?:MIN|MAX)\s*\([^()]{1,200}\))"
        r"\s*[.;]?\s*(?=;|\n|$)",
        instructions,
        re.I,
    )
    if len(formulas) != 1:
        return []
    try:
        expected = _expression(formulas[0])
    except (ValueError, TypeError, SqlglotError):
        return []
    if not isinstance(expected, (exp.Min, exp.Max)) or not isinstance(expected.this, exp.Column):
        return []
    column = expected.this
    candidates = [
        (_identifier_key(table), _identifier_key(name))
        for table, names in schema.items()
        for name in names
        if _identifier_key(name) == _identifier_key(column.name)
        and (not column.table or _identifier_key(table) == _identifier_key(column.table))
    ]
    if len(candidates) != 1:
        return []
    tree = parse_one(sql, read="sqlite")
    if not isinstance(tree, exp.Select) or len(tree.selects) != 1:
        return []
    scope = next((s for s in traverse_scope(tree) if s.expression is tree), None)
    if scope is None:
        return []
    node = tree.selects[0]
    node = _unparen(node.this if isinstance(node, exp.Alias) else node)
    converted = isinstance(node, exp.Cast)
    if converted:
        node = _unparen(node.this)
    if type(node) is not type(expected):
        return []
    argument = _unparen(node.this)
    if isinstance(argument, exp.Cast):
        converted = True
        argument = _unparen(argument.this)
    if not converted or not isinstance(argument, exp.Column) or argument.db or argument.catalog:
        return []
    sources = []
    for alias, (_, source) in scope.selected_sources.items():
        if argument.table and _identifier_key(argument.table) != _identifier_key(alias):
            continue
        # A derived source might expose the same name; do not infer its physical origin.
        if not isinstance(source, exp.Table) or source.db or source.catalog:
            return []
        sources.extend(
            (_identifier_key(table), _identifier_key(name))
            for table, names in schema.items()
            for name in names
            if _identifier_key(source.name) == _identifier_key(table)
            and _identifier_key(argument.name) == _identifier_key(name)
        )
    if sources == candidates:
        return ["public_extremum_representation"]
    return []


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
