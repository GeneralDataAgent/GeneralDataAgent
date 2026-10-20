"""Conservative structural checks against an explicit, public task interpretation.

These checks catch contradictions; passing is not a proof that SQL answers natural language.
SqlGuard remains the independent read-only and relation-scope authority.
"""

import math

from sqlglot import exp, parse_one

from generaldataagent.contracts.analysis import AnalysisTaskSpec, AnswerSpec


class SemanticError(ValueError):
    pass


class ColumnScopeError(SemanticError):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("unresolved or ambiguous scoped SQL column; check schema")


def inspect_semantics(sql: str, spec: AnalysisTaskSpec) -> list[str]:
    tree = parse_one(sql, read="sqlite")
    issues = []
    output = [item.alias_or_name for item in tree.selects]
    if spec.answer.columns and output != spec.answer.columns:
        issues.append("output_projection")
    if spec.answer.ties == "all" and tree.args.get("limit"):
        issues.append("ties_may_be_truncated")
    if spec.answer.shape == "set" and tree.args.get("limit"):
        issues.append("set_may_be_truncated")
    order = tree.args.get("order")
    if spec.order_by and spec.order_direction:
        if not order or not order.expressions:
            issues.append("missing_order")
        else:
            first = order.expressions[0]
            if first.this.sql(dialect="sqlite") != spec.order_by:
                issues.append("order_field")
            if bool(first.args.get("desc")) != (spec.order_direction == "desc"):
                issues.append("order_direction")
    # A joined COUNT(*) contradicts an explicitly requested distinct entity count.
    if spec.count_basis == "entities":
        for select in tree.find_all(exp.Select):
            # Do not combine a join in one scope with a count in another. In particular,
            # COUNT(*) over a DISTINCT entity subquery is a valid deduplication plan.
            if select.args.get("joins") and any(
                count.find_ancestor(exp.Select) is select
                and not isinstance(count.this, exp.Distinct)
                for count in select.find_all(exp.Count)
            ):
                issues.append("joined_entity_count_requires_distinct")
    # Only syntactic denominator safety can be certified without executing task-specific logic.
    if spec.denominator:
        divisions = list(tree.find_all(exp.Div))
        if not divisions:
            issues.append("missing_ratio")
        elif any(not d.expression.find(exp.Nullif) for d in divisions):
            issues.append("zero_denominator_unprotected")
    from generaldataagent.tools.semantic_requirements import (
        check_public_change_direction,
        check_public_rank_and_age,
        check_requirements,
    )

    try:
        issues.extend(check_requirements(sql, spec.requirements))
        issues.extend(check_public_change_direction(sql, spec.question))
        issues.extend(check_public_rank_and_age(sql, spec.question))
    except (ValueError, TypeError):
        issues.append("invalid_semantic_requirement")
    return sorted(set(issues))


def validate_rows(columns, rows, answer: AnswerSpec, *, complete: bool = True) -> list[str]:
    issues = []
    if not complete:
        issues.append("incomplete_result")
    if answer.columns and list(columns) != answer.columns:
        issues.append("output_projection")
    if not rows and answer.empty == "reject":
        issues.append("empty_result")
    if answer.shape == "scalar" and (len(rows) != 1 or len(columns) != 1):
        issues.append("scalar_cardinality")
    if any(len(row) != len(columns) for row in rows):
        issues.append("row_width")
    if answer.shape == "set" and len({tuple(row) for row in rows}) != len(rows):
        issues.append("duplicate_entities")
    for row in rows:
        for value in row:
            if value is None and not answer.nullable:
                issues.append("unexpected_null")
            if isinstance(value, (int, float)):
                if not math.isfinite(value):
                    issues.append("nonfinite_value")
                if answer.minimum is not None and value < answer.minimum:
                    issues.append("below_range")
                if answer.maximum is not None and value > answer.maximum:
                    issues.append("above_range")
    return sorted(set(issues))


def assemble_answers(bindings: dict, evidence: dict[int, dict], answer: AnswerSpec) -> dict:
    """Resolve each answer from full successful evidence; preserve full precision upstream."""
    if not bindings or (answer.columns and set(bindings) != set(answer.columns)):
        raise SemanticError("every answer field needs an evidence binding")
    from generaldataagent.tools.answer_fields import format_decimal, validate_field_value

    output = {}
    for key, (step, path) in bindings.items():
        if step not in evidence or not path:
            raise SemanticError("unknown evidence or empty evidence path")
        value = evidence[step]
        try:
            for part in path:
                if isinstance(value, (list, tuple)):
                    if type(part) is not int or part < 0:
                        raise SemanticError("invalid evidence index")
                elif not isinstance(value, dict) or not isinstance(part, str):
                    raise SemanticError("invalid evidence path")
                value = value[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise SemanticError("evidence path not found") from exc
        dependencies = evidence[step].get("data_dependent_columns")
        if dependencies is not None and (
            len(path) != 3
            or path[0] != "rows"
            or type(path[2]) is not int
            or path[2] < 0
            or path[2] >= len(dependencies)
            or dependencies[path[2]] is not True
        ):
            raise SemanticError("data_dependent_answer_required")
        field = answer.fields.get(key)
        if field is not None:
            try:
                value = validate_field_value(value, field, evidence[step], path)
            except ValueError as exc:
                raise SemanticError(str(exc)) from None
        if isinstance(value, list) and path == ["items"]:
            result = evidence[step]
            if (
                result.get("complete") is not True
                or result.get("total_count") != len(value)
                or (answer.shape != "set" and (field is None or field.kind != "set"))
            ):
                raise SemanticError("collection completeness not verified")
            checks = validate_rows(
                [key],
                [(item,) for item in value],
                answer.model_copy(update={"columns": [key], "shape": "set"}),
            )
            if checks:
                raise SemanticError(",".join(checks))
            output[key] = value
            continue
        if isinstance(value, (dict, list, tuple)):
            raise SemanticError("bind individual scalar fields, not previews or containers")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise SemanticError("unsupported evidence value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise SemanticError("nonfinite evidence")
            decimals = field.decimals if field and field.decimals is not None else answer.decimals
            if field and field.kind == "integer" and field.decimals is None:
                decimals = (
                    None  # A precision declared for another numeric field cannot change this type.
                )
            if decimals is not None:
                value = format_decimal(value, decimals)
        output[key] = value
    # Validate numeric ranges before converting numeric values to formatted strings.
    unrounded = AnswerSpec.model_validate(answer.model_dump() | {"columns": [], "shape": "rows"})
    for key, (step, path) in bindings.items():
        raw = evidence[step]
        for part in path:
            raw = raw[part]
        if isinstance(raw, list):
            continue
        if validate_rows([key], [(raw,)], unrounded):
            raise SemanticError("answer value violates contract")
    return output


def collect_complete(database, sql: str, *, allowed_relations: set[str]) -> dict:
    """A bounded complete one-column collection; over-limit queries fail, never truncate."""
    from generaldataagent.tools.sql_guard import SqlGuard
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    SqlGuard(dialect="sqlite").inspect(sql, allowed_relations=allowed_relations)
    validate_column_scope(database, sql, allowed_relations=allowed_relations)
    tree = parse_one(sql, read="sqlite")
    if tree.find(exp.Limit) or tree.find(exp.Offset):
        raise SemanticError("complete collections cannot use LIMIT or OFFSET")
    result = SQLiteReadOnlyExecutor(database, max_rows=10000, timeout_seconds=5).execute(sql)
    if len(result.columns) != 1:
        raise SemanticError("complete collection requires one explicit output column")
    if not all(
        sql_data_dependencies(sql, list(result.columns), allowed_relations=allowed_relations)
    ):
        raise SemanticError("data_dependent_answer_required")
    items = [row[0] for row in result.rows]
    if len(set(items)) != len(items):
        raise SemanticError("complete set contains duplicates; use distinct entity grain")
    return {"items": items, "total_count": len(items), "complete": True}


def validate_column_scope(database, sql: str, *, allowed_relations: set[str]) -> None:
    """Resolve identifiers before SQLite's double-quoted-string compatibility can mask typos."""
    import sqlite3

    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.schema import MappingSchema

    from generaldataagent.tools.sql_guard import SqlGuard

    SqlGuard(dialect="sqlite").inspect(sql, allowed_relations=allowed_relations)
    schema = {}
    physical_fields = set()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for table in sorted(allowed_relations):
            quoted = '"' + table.replace('"', '""') + '"'
            schema[table] = {
                row[1]: "UNKNOWN" for row in db.execute(f"PRAGMA table_info({quoted})")
            }
            physical_fields.update(schema[table])
            for alias in ("rowid", "_rowid_", "oid"):
                if alias in {name.lower() for name in schema[table]}:
                    continue
                try:
                    db.execute(
                        f'SELECT _scope_probe."{alias}" FROM {quoted} AS _scope_probe LIMIT 0'
                    )
                except sqlite3.Error:
                    continue  # WITHOUT ROWID tables do not have these implicit fields.
                schema[table][alias] = "UNKNOWN"
    try:
        qualify(
            parse_one(sql, read="sqlite"),
            dialect="sqlite",
            schema=MappingSchema(
                {
                    table.lower(): {column.lower(): kind for column, kind in columns.items()}
                    for table, columns in schema.items()
                },
                dialect="sqlite",
                normalize=False,
            ),
            validate_qualify_columns=True,
            infer_schema=False,
        )
    except OptimizeError as exc:
        import difflib
        import re

        matched = re.search(r"Column ['\"]([\w .-]{1,120})['\"]", str(exc))
        unknown = matched.group(1).split(".")[-1] if matched else ""
        physical = sorted(physical_fields)
        matched_names = (
            difflib.get_close_matches(
                unknown.casefold(), sorted({name.casefold() for name in physical}), n=5, cutoff=0.35
            )
            if unknown
            else []
        )
        candidates = [
            name for folded in matched_names for name in physical if name.casefold() == folded
        ][:5]
        raise ColumnScopeError(candidates) from None


def sql_data_dependencies(
    sql: str, columns: list[str], *, allowed_relations: set[str]
) -> list[bool]:
    """Detect syntactic data dependencies, not correctness of a calculation or interpretation.

    A successful SELECT of model-supplied constants cannot become full-data answer evidence.
    COUNT(*) depends on source rows even though column lineage has no physical column leaf.
    Unsupported lineage remains unverified and cannot certify a bound answer.
    """
    from sqlglot.errors import SqlglotError
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.scope import Scope, traverse_scope

    allowed = {name.casefold() for name in allowed_relations}

    def row_dependency(scope, seen):
        if scope is None or id(scope) in seen:
            return False
        seen = seen | {id(scope)}
        return any(
            SqlGuard._relation_name(source).casefold() in allowed
            if isinstance(source, exp.Table)
            else row_dependency(source, seen)
            if isinstance(source, Scope)
            else False
            for _, source in scope.selected_sources.values()
        )

    from generaldataagent.tools.sql_guard import SqlGuard

    tree = parse_one(sql, read="sqlite")
    if len(tree.selects) != len(columns) or any(isinstance(p, exp.Star) for p in tree.selects):
        return [False] * len(columns)
    names = ["_evidence_" + str(index) for index in range(len(columns))]
    for projection, alias in zip(list(tree.selects), names, strict=True):
        projection.replace(exp.alias_(projection.unalias().copy(), alias, quoted=True))
    output = []
    for name in names:
        try:
            node = lineage(name, tree, dialect="sqlite", trim_selects=False)
            dependent = False
            for item in node.walk():
                if isinstance(item.expression, exp.Table):
                    dependent |= SqlGuard._relation_name(item.expression).casefold() in allowed
                for count in item.expression.find_all(exp.Count):
                    argument = count.this
                    row_count = isinstance(argument, (exp.Star, exp.Literal))
                    if not row_count:
                        continue
                    select = count.find_ancestor(exp.Select)
                    # Preserve lexical CTE visibility, but never borrow row sources from an
                    # enclosing SELECT for a constant-only scalar subquery's COUNT.
                    count_scope = next(
                        (
                            scope
                            for scope in traverse_scope(count.root())
                            if scope.expression is select
                        ),
                        None,
                    )
                    dependent |= row_dependency(count_scope, set())
            output.append(dependent)
        except (SqlglotError, ValueError, TypeError):
            output.append(False)
    return output


def check_percentage_result(sql: str, question: str, instructions: str, rows) -> list[str]:
    """Narrow fraction-of-population bounds; growth/change percentages may be unbounded."""
    import re

    public = question + "\n" + instructions
    if not re.search(r"\bpercentage\s+of\b|\bwhat\s+percent\b", question, re.I):
        return []
    if re.search(r"increas|decreas|growth|change|difference|return\b", question, re.I):
        return []
    if re.search(r"\b(?:return|report|use)\s+(?:null|none|not applicable)\b", public, re.I):
        return []
    projections = parse_one(sql, read="sqlite").selects
    if any(item.is_star for item in projections):
        return []

    def population_count(node):
        while isinstance(node, (exp.Paren, exp.Cast, exp.Nullif)):
            node = node.this
        if isinstance(node, exp.Count):
            return True
        if isinstance(node, exp.Sum) and isinstance(node.this, exp.Case):
            branches = [branch.args.get("true") for branch in node.this.args.get("ifs", [])]
            branches.append(node.this.args.get("default"))
            return bool(branches) and all(
                branch is None
                or isinstance(branch, exp.Null)
                or isinstance(branch, exp.Literal)
                and not branch.is_string
                and branch.this in {"0", "1"}
                for branch in branches
            )
        return False

    indices = [
        index
        for index, item in enumerate(projections)
        if any(population_count(division.expression) for division in item.find_all(exp.Div))
    ]
    issues = []
    for row in rows:
        for index in indices:
            value = row[index]
            if value is None:
                issues.append("undefined_percentage_check_population")
            elif type(value) in {int, float} and not 0 <= value <= 100:
                issues.append("percentage_out_of_range")
    return sorted(set(issues))
