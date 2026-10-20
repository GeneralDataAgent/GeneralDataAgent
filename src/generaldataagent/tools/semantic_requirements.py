"""Fail-closed checks of declared predicate location, date bounds, and ratio operands.

Public excerpts establish provenance, not correctness of a model's interpretation. Checks are
structural and deliberately reject unsupported algebra rather than claim semantic equivalence.
"""

from decimal import Decimal

from sqlglot import exp, parse, parse_one

from generaldataagent.contracts.semantic_requirements import SemanticRequirements


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _normalized(node):
    def normalize(part):
        if isinstance(part, exp.Paren):
            return part.this
        if isinstance(part, exp.Identifier):
            return exp.to_identifier(part.name.casefold(), quoted=False)
        if isinstance(part, exp.Literal) and not part.is_string:
            return exp.Literal.number(format(Decimal(part.this).normalize(), "f"))
        return part

    return node.copy().transform(normalize).sql(dialect="sqlite", pretty=False)


def _expression(text):
    statements = parse("SELECT " + text, read="sqlite")
    if len(statements) != 1 or len(statements[0].selects) != 1:
        raise ValueError("one semantic expression required")
    return statements[0].selects[0]


def _scope(tree, name):
    if name == "root":
        return tree if isinstance(tree, exp.Select) else None
    candidates = [
        node.this
        for cls in (exp.CTE, exp.Subquery)
        for node in tree.find_all(cls)
        if node.alias.casefold() == name.casefold()
    ]
    return candidates[0] if len(candidates) == 1 and isinstance(candidates[0], exp.Select) else None


def _conjuncts(node):
    node = _unparen(node)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _conditions(select, clause):
    if clause == "join":
        nodes = [join.args.get("on") for join in select.args.get("joins", [])]
    else:
        wrapper = select.args.get(clause)
        nodes = [wrapper.this] if wrapper else []
    return {_normalized(term) for node in nodes if node is not None for term in _conjuncts(node)}


def _numeric(node):
    node = _unparen(node)
    if isinstance(node, exp.Literal) and not node.is_string:
        return Decimal(node.this)
    return None


def _scaled(node):
    node = _unparen(node)
    if isinstance(node, exp.Mul):
        for constant, other in ((node.this, node.expression), (node.expression, node.this)):
            value = _numeric(constant)
            if value is not None:
                expression, factor = _scaled(other)
                return expression, factor * value
    return node, Decimal(1)


def _ratio(node):
    node, scale = _scaled(node)
    if not isinstance(node, exp.Div):
        return None
    numerator, inner_scale = _scaled(node.this)
    denominator = _unparen(node.expression)
    guarded = isinstance(denominator, exp.Nullif) and _numeric(denominator.expression) == 0
    if guarded:
        denominator = denominator.this
    return numerator, denominator, scale * inner_scale, guarded


def check_requirements(sql: str, requirements: SemanticRequirements) -> list[str]:
    tree = parse_one(sql, read="sqlite")
    issues = []
    for item in requirements.predicates:
        select = _scope(tree, item.scope)
        if select is None:
            issues.append("predicate_scope")
            continue
        expected = {_normalized(term) for term in _conjuncts(_expression(item.expression))}
        if not expected <= _conditions(select, item.clause):
            issues.append("predicate_placement_or_population")
    for item in requirements.dates:
        select = _scope(tree, item.scope)
        if select is None:
            issues.append("date_scope")
            continue
        column = _expression(item.column)
        predicates = []
        for value, inclusive, lower in (
            (item.start, item.start_inclusive, True),
            (item.end, item.end_inclusive, False),
        ):
            encoded = value if item.encoding == "iso_date" else value.replace("-", "")
            literal = (
                exp.Literal.number(encoded)
                if item.encoding == "compact_integer"
                else exp.Literal.string(encoded)
            )
            cls = (
                (exp.GTE if inclusive else exp.GT) if lower else (exp.LTE if inclusive else exp.LT)
            )
            predicates.append(cls(this=column.copy(), expression=literal))
        if not {_normalized(p) for p in predicates} <= _conditions(select, "where"):
            issues.append("date_boundary_or_encoding")
    for item in requirements.ratios:
        select = _scope(tree, item.scope)
        if select is None:
            issues.append("ratio_scope")
            continue
        outputs = [
            p for p in select.selects if p.alias_or_name.casefold() == item.output_column.casefold()
        ]
        shape = (
            _ratio(outputs[0].this if isinstance(outputs[0], exp.Alias) else outputs[0])
            if len(outputs) == 1
            else None
        )
        if shape is None:
            issues.append("ratio_projection")
            continue
        numerator, denominator, scale, guarded = shape
        if _normalized(numerator) != _normalized(_expression(item.numerator)):
            issues.append("ratio_numerator")
        if _normalized(denominator) != _normalized(_expression(item.denominator)):
            issues.append("ratio_denominator_population")
        if scale != item.multiplier:
            issues.append("ratio_scale")
        if item.zero_guard and not guarded:
            issues.append("ratio_zero_guard")
    return sorted(set(issues))


def validate_requirement_sources(
    requirements: SemanticRequirements, question: str, instructions: str
):
    public = question + "\n" + instructions
    for collection in (requirements.predicates, requirements.dates, requirements.ratios):
        for item in collection:
            if item.source_excerpt not in public:
                raise ValueError("semantic requirement needs an exact public source excerpt")


def check_public_change_direction(sql: str, question: str) -> list[str]:
    """Resolve explicit 'from DATE to DATE' reference periods through scalar subqueries.

    Only plain columns, direct table aliases and conjunctive date equalities are resolved.
    Other forms are left unclassified, never guessed.
    No benchmark identity, label, or reference query is used.
    """
    import re
    from datetime import date

    if not re.search(
        r"\b(?:increase rate|growth rate|percentage change|percent change)\b", question, re.I
    ):
        return []
    matched = re.search(
        r"\bfrom\s+(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s+to\s+"
        r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b",
        question,
        re.I,
    )
    if not matched:
        return []

    def canonical(value):
        try:
            parts = re.split(r"[/-]", value)
            return date(*map(int, parts)).isoformat()
        except (ValueError, TypeError):
            return None

    start, end = (canonical(value) for value in matched.groups())
    if start is None or end is None or start == end:
        return []
    tree = parse_one(sql, read="sqlite")
    sources = {}
    for cls in (exp.CTE, exp.Subquery):
        for node in tree.find_all(cls):
            sources.setdefault(node.alias.casefold(), []).append(node.this)

    def period(column):
        column = _unparen(column)
        if not isinstance(column, exp.Column):
            return None
        candidates = sources.get(column.table.casefold(), [])
        if candidates:
            if len(candidates) != 1 or not isinstance(candidates[0], exp.Select):
                return None
            select = candidates[0]
            projections = [
                p for p in select.selects if p.alias_or_name.casefold() == column.name.casefold()
            ]
            if len(projections) != 1:
                return None
            projection = projections[0]
            source = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(source, exp.Column):
                return None
        else:
            # Also resolve aliases of directly joined tables. Never borrow a date condition
            # from a nested scope with the same alias, or infer unqualified multi-table fields.
            select = column.find_ancestor(exp.Select)
            if select is not tree or not column.table:
                return None
            local = [
                table
                for table in select.find_all(exp.Table)
                if table.find_ancestor(exp.Select) is select
                and table.alias_or_name.casefold() == column.table.casefold()
            ]
            if len(local) != 1:
                return None
            source = column
        where = select.args.get("where")
        dates = set()
        conditions = _conjuncts(where.this) if where else []
        for join in select.args.get("joins", []):
            if not join.args.get("side") and join.args.get("on") is not None:
                conditions.extend(_conjuncts(join.args["on"]))
        for predicate in conditions:
            if not isinstance(predicate, exp.EQ):
                continue
            for field, literal in (
                (predicate.this, predicate.expression),
                (predicate.expression, predicate.this),
            ):
                if (
                    isinstance(field, exp.Column)
                    and field.table.casefold() == source.table.casefold()
                    and re.search(r"date|day|time", field.name, re.I)
                    and isinstance(literal, exp.Literal)
                    and literal.is_string
                ):
                    value = canonical(literal.this)
                    if value in {start, end}:
                        dates.add(value)
        return next(iter(dates)) if len(dates) == 1 else None

    issues = []
    for projection in tree.selects:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        parts = _ratio(node)
        if parts is None:
            continue
        numerator, denominator, _, _ = parts
        reference = period(denominator)
        if reference is not None and reference != start:
            issues.append("change_reference_period")
        numerator = _unparen(numerator)
        if isinstance(numerator, exp.Sub):
            left, right = period(numerator.this), period(numerator.expression)
            if left is not None and right is not None and (left != end or right != start):
                issues.append("change_direction")
    return sorted(set(issues))


def check_public_rank_and_age(sql: str, question: str) -> list[str]:
    """Enforce narrow, explicit public ranking and completed-age conventions."""
    import re

    tree = parse_one(sql, read="sqlite")
    issues = []
    if (
        re.search(r"\bamong\s+(?:the\s+)?top\s+\d+", question, re.I)
        and not re.search(r"\bexactly\b|\bbreak\s+ties\b", question, re.I)
        and any(isinstance(w.this, exp.RowNumber) for w in tree.find_all(exp.Window))
    ):
        issues.append("rank_ties_require_rank")
    if re.search(r"\bamong\s+(?:the\s+)?top\s+\d+", question, re.I) and not re.search(
        r"non.?null|exclude|missing|null|known scores|valid scores", question, re.I
    ):
        for select in tree.find_all(exp.Select):
            ordering = {
                column.name.casefold()
                for window in select.find_all(exp.Window)
                for order in [window.args.get("order")]
                if order is not None
                for column in order.find_all(exp.Column)
            }
            where = select.args.get("where")
            for condition in _conjuncts(where.this) if where else []:
                if (
                    isinstance(condition, exp.Not)
                    and isinstance(condition.this, exp.Is)
                    and isinstance(condition.this.expression, exp.Null)
                    and isinstance(condition.this.this, exp.Column)
                    and condition.this.this.name.casefold() in ordering
                ):
                    issues.append("unrequested_rank_population_filter")
    if re.search(r"\bage\b|\bhow old\b", question, re.I) and re.search(
        r"arriv|admi", question, re.I
    ):
        for projection in tree.selects:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            code = expression.sql(dialect="sqlite")
            # Recognize the incomplete year-only shortcut; alternative age algorithms remain
            # unclassified instead of being falsely certified.
            if (
                "%Y" in code
                and isinstance(_unparen(expression), exp.Sub)
                and not any(pattern in code for pattern in ("%m", "%d", "%j", "%J"))
            ):
                issues.append("age_requires_birthday_boundary")
    return sorted(set(issues))
