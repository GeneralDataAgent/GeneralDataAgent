"""Fail-closed checks of declared predicate location, date bounds, and ratio operands.

Public excerpts establish provenance, not correctness of a model's interpretation. Checks are
structural and deliberately reject unsupported algebra rather than claim semantic equivalence.
"""

from decimal import Decimal

from sqlglot import exp, parse, parse_one

from generaldataagent.contracts.semantic_requirements import SemanticRequirements


def _identifier_key(value):
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _normalized(node):
    def normalize(part):
        if isinstance(part, exp.Paren):
            return part.this
        if isinstance(part, exp.Identifier):
            return exp.to_identifier(_identifier_key(part.name), quoted=True)
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
        if _identifier_key(node.alias) == _identifier_key(name)
    ]
    if candidates:
        return (
            candidates[0]
            if len(candidates) == 1 and isinstance(candidates[0], exp.Select)
            else None
        )
    if isinstance(tree, exp.Select):
        from sqlglot.optimizer.scope import traverse_scope

        root_scope = next(
            (scope for scope in traverse_scope(tree) if scope.expression is tree), None
        )
        if root_scope is not None:
            # A uniquely named physical source of the root SELECT identifies that
            # SELECT, not the table's nested appearances or another query scope.
            sources = [
                source
                for _, source in root_scope.selected_sources.values()
                if isinstance(source, exp.Table)
                and _identifier_key(name)
                in {
                    _identifier_key(source.name),
                    _identifier_key(source.alias_or_name),
                }
            ]
            if len(sources) == 1:
                return tree
    return None


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
            p
            for p in select.selects
            if _identifier_key(p.alias_or_name) == _identifier_key(item.output_column)
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
            sources.setdefault(_identifier_key(node.alias), []).append(node.this)

    def period(column):
        column = _unparen(column)
        if not isinstance(column, exp.Column):
            return None
        candidates = sources.get(_identifier_key(column.table), [])
        if candidates:
            if len(candidates) != 1 or not isinstance(candidates[0], exp.Select):
                return None
            select = candidates[0]
            projections = [
                p
                for p in select.selects
                if _identifier_key(p.alias_or_name) == _identifier_key(column.name)
            ]
            if len(projections) != 1:
                return None
            projection = projections[0]
            source = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
                # A derived output may itself be a scalar lookup. Resolve its own date
                # equality, rather than losing the period behind the output alias.
                select = source.this
                if len(select.selects) != 1 or select.args.get("group"):
                    return None
                source = select.selects[0]
                source = source.this if isinstance(source, exp.Alias) else source
                tables = [
                    table
                    for table in select.find_all(exp.Table)
                    if table.find_ancestor(exp.Select) is select
                ]
                if (
                    len(tables) != 1
                    or select.args.get("joins")
                    or not isinstance(source, exp.Column)
                    or (
                        source.table
                        and _identifier_key(source.table)
                        != _identifier_key(tables[0].alias_or_name)
                    )
                ):
                    return None
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
                and _identifier_key(table.alias_or_name) == _identifier_key(column.table)
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
                    and _identifier_key(field.table) == _identifier_key(source.table)
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


def check_public_rank_and_age(sql: str, question: str, instructions: str = "") -> list[str]:
    """Enforce narrow, explicit public ranking and completed-age conventions."""
    import re

    tree = parse_one(sql, read="sqlite")
    issues = []
    public = question + "\n" + instructions

    def affirmative_clauses(text):
        # A negated example is not an instruction to apply that example's policy.
        return "\n".join(
            clause
            for clause in re.split(r"[;\n]", text)
            if not re.search(r"\b(?:do not|don't|never|must not|not to)\b", clause, re.I)
        )

    policy_text = affirmative_clauses(public)
    if tree.args.get("limit") and re.search(
        r"\b(?:preserve|retain|include|return)\s+(?:all\s+)?ties\b"
        r"|\ball\s+(?:entities|people|rows|records|items)\s+tied\b",
        policy_text,
        re.I,
    ):
        issues.append("ties_may_be_truncated")
    if (
        re.search(r"\bamong\s+(?:the\s+)?top\s+\d+", question, re.I)
        and not re.search(r"\bexactly\b|\bbreak\s+ties\b", policy_text, re.I)
        and any(isinstance(w.this, exp.RowNumber) for w in tree.find_all(exp.Window))
    ):
        issues.append("rank_ties_require_rank")
    if re.search(r"\bamong\s+(?:the\s+)?top\s+\d+", question, re.I) and not re.search(
        r"\bnon[- ]?null\b|\b(?:exclude|ignore|omit)\s+(?:rows?\s+with\s+)?(?:missing|null)\b"
        r"|\b(?:known|valid)\s+scores\b",
        policy_text,
        re.I,
    ):
        for select in tree.find_all(exp.Select):
            ordering = {
                _identifier_key(column.name)
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
                    and _identifier_key(condition.this.this.name) in ordering
                ):
                    issues.append("unrequested_rank_population_filter")
    year_policy = affirmative_clauses(instructions)
    # Recognize prose formulas with redundant grouping without executing or certifying
    # that notation. An explicit calendar correction must still be honored.
    left_year = r"\byear\s*\([^)]{1,100}\)(?:\s*\))*"
    right_year = r"(?:\(\s*)*\byear\s*\("
    year_formula = re.search(
        left_year
        + r"\s*-\s*"
        + right_year
        + r"|\bsubtract\s*\(\s*(?:\(\s*)*"
        + left_year
        + r"\s*,\s*"
        + right_year,
        year_policy,
        re.I,
    )
    outside_year_fields = re.sub(r"\byear\s*\([^)]{1,100}\)", "YEAR_FIELD", year_policy, flags=re.I)
    calendar_correction = re.search(
        r"\bcompleted\s+years\b"
        r"|\b(?:account|adjust|correct)\s+for\s+(?:the\s+)?(?:month|day)\b"
        r"|\b(?:minus|subtract)\s+(?:one|1)\b[^;\n]{0,120}\b(?:months?|days?|birthday)\b"
        r"|-\s*\([^)]{0,120}(?:strftime|month|day)\s*\("
        r"|-\s*case\s+when\b[^;\n]{0,120}\b(?:strftime|month|day)\s*\(",
        outside_year_fields,
        re.I,
    )
    explicit_year_difference = year_formula and not calendar_correction
    if (
        not explicit_year_difference
        and re.search(r"\bage\b|\bhow old\b", question, re.I)
        and re.search(r"arriv|admi", question, re.I)
    ):
        for projection in tree.selects:
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            while isinstance(expression, (exp.Cast, exp.Paren)):
                expression = expression.this
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


def check_public_all_count_population(sql: str, question: str, instructions: str) -> list[str]:
    """Catch a narrow contradiction of an explicit SUM(field)/COUNT(all ...) formula.

    Only a direct ratio and a same-scope null exclusion of its sum field are classified.
    Other formulas, aliases through subqueries, and explicitly restricted populations stay
    unclassified. No entity, dataset, reference query or expected value is consulted.
    """
    import re

    from sqlglot.errors import SqlglotError

    match = re.search(
        r"\bDIVIDE\s*\(\s*SUM\s*\(([^()]{1,160})\)\s*,\s*COUNT\s*\(\s*all\b",
        instructions,
        re.I,
    )
    if not match or re.search(
        r"\b(?:where|with|whose|having|known|valid|exclude|missing)\b|\bnon[- ]?null\b",
        question + "\n" + instructions,
        re.I,
    ):
        return []
    try:
        field = _expression(match[1])
    except (SqlglotError, ValueError, TypeError):
        return []
    if not isinstance(field, exp.Column):
        return []
    tree = parse_one(sql, read="sqlite")
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        excluded = (
            {
                _normalized(term.this.this)
                for term in _conjuncts(where.this)
                if where is not None
                if isinstance(term, exp.Not)
                and isinstance(term.this, exp.Is)
                and isinstance(term.this.expression, exp.Null)
            }
            if where
            else set()
        )
        for projection in select.selects:
            for division in projection.find_all(exp.Div):
                parts = _ratio(division)
                if parts is None:
                    continue
                numerator, denominator, _, _ = parts
                if (
                    isinstance(numerator, exp.Sum)
                    and isinstance(numerator.this, exp.Column)
                    and _identifier_key(numerator.this.name) == _identifier_key(field.name)
                    and isinstance(denominator, exp.Count)
                    and isinstance(denominator.this, (exp.Star, exp.Literal))
                    and _normalized(numerator.this) in excluded
                ):
                    return ["unrequested_all_count_null_filter"]
    return []


def check_public_age_direction(sql: str, question: str, instructions: str) -> list[str]:
    """Check direct extrema only when public text explicitly orders an age encoding.

    Field names alone do not establish date encoding. Derived/ambiguous sources,
    transformed values and unrelated nested scopes remain unclassified.
    """
    import re

    from sqlglot.optimizer.scope import traverse_scope

    oldest = bool(re.search(r"\boldest\b", question, re.I))
    youngest = bool(re.search(r"\byoungest\b", question, re.I))
    if oldest == youngest:
        return []
    public = "\n".join(
        clause
        for clause in re.split(r"[;\n]", instructions)
        if not re.search(r"\b(?:do not|don't|never|must not|not to)\b", clause, re.I)
    )
    policies = re.findall(
        r"\b(?:larger|higher|greater)\s+(?:the\s+)?"
        r"(?:value\s+of\s+)?([A-Za-z_]\w*)\s*(?:value)?\s*,\s*"
        r"(?:the\s+)?(younger|older)\b",
        public,
        re.I,
    )
    if len(set((_identifier_key(field), direction.lower()) for field, direction in policies)) != 1:
        return []
    field, meaning = policies[0]
    # If larger means younger, oldest is a minimum; if larger means older, invert.
    want_max = youngest == (meaning.lower() == "younger")
    tree = parse_one(sql, read="sqlite")
    if not isinstance(tree, exp.Select):
        return []
    scopes = {id(scope.expression): scope for scope in traverse_scope(tree)}

    def physical_column(column, select):
        if not isinstance(column, exp.Column) or _identifier_key(column.name) != _identifier_key(
            field
        ):
            return None
        scope = scopes.get(id(select))
        if scope is None:
            return None
        sources = scope.selected_sources
        if column.table:
            matches = [
                source
                for alias, (_, source) in sources.items()
                if _identifier_key(alias) == _identifier_key(column.table)
            ]
        else:
            matches = [source for _, source in sources.values()] if len(sources) == 1 else []
        if len(matches) != 1 or not isinstance(matches[0], exp.Table):
            return None
        table = matches[0]
        return tuple(_identifier_key(value) for value in (table.catalog, table.db, table.name))

    order = tree.args.get("order")
    if tree.args.get("limit") and order and order.expressions:
        first = order.expressions[0]
        if (
            physical_column(first.this, tree) is not None
            and bool(first.args.get("desc")) != want_max
        ):
            return ["public_age_order_direction"]
    where = tree.args.get("where")
    for term in _conjuncts(where.this) if where else []:
        if not isinstance(term, exp.EQ):
            continue
        for column, query in ((term.this, term.expression), (term.expression, term.this)):
            outer_source = physical_column(column, tree)
            if outer_source is None or not isinstance(query, exp.Subquery):
                continue
            select = query.this
            if (
                not isinstance(select, exp.Select)
                or select.args.get("group")
                or len(select.selects) != 1
            ):
                continue
            aggregate = _unparen(select.selects[0])
            if isinstance(aggregate, exp.Alias):
                aggregate = _unparen(aggregate.this)
            if (
                isinstance(aggregate, (exp.Max, exp.Min))
                and physical_column(aggregate.this, select) == outer_source
                and isinstance(aggregate, exp.Max) != want_max
            ):
                return ["public_age_order_direction"]
    return []
