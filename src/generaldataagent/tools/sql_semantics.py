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
    items = [row[0] for row in result.rows]
    if len(set(items)) != len(items):
        raise SemanticError("complete set contains duplicates; use distinct entity grain")
    return {"items": items, "total_count": len(items), "complete": True}


def validate_column_scope(database, sql: str, *, allowed_relations: set[str]) -> None:
    """Resolve identifiers before SQLite's double-quoted-string compatibility can mask typos."""
    import sqlite3

    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.qualify import qualify

    from generaldataagent.tools.sql_guard import SqlGuard

    SqlGuard(dialect="sqlite").inspect(sql, allowed_relations=allowed_relations)
    schema = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for table in sorted(allowed_relations):
            quoted = '"' + table.replace('"', '""') + '"'
            schema[table] = {
                row[1]: "UNKNOWN" for row in db.execute(f"PRAGMA table_info({quoted})")
            }
    try:
        qualify(
            parse_one(sql, read="sqlite"),
            dialect="sqlite",
            schema=schema,
            validate_qualify_columns=True,
            infer_schema=False,
        )
    except OptimizeError as exc:
        import difflib
        import re

        matched = re.search(r"Column ['\"]([\w .-]{1,120})['\"]", str(exc))
        unknown = matched.group(1).split(".")[-1] if matched else ""
        physical = sorted({column for columns in schema.values() for column in columns})
        candidates = (
            difflib.get_close_matches(unknown, physical, n=5, cutoff=0.35) if unknown else []
        )
        raise ColumnScopeError(candidates) from None
