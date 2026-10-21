"""Compile scoped entity counts and tie-preserving ranks from a bounded relational plan.

The compiler guarantees declared plan semantics, not that a model chose the right business
interpretation. Only physical fields, fixed operators and public filter literals are accepted.
"""

import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from generaldataagent.contracts.analysis import AnalysisTaskSpec, AnswerSpec
from generaldataagent.contracts.query_plan import SemanticQueryPlan
from generaldataagent.tools.sql_guard import SqlGuard


def semantic_plan_error_code(error: ValueError) -> str:
    """Classify fixed compiler diagnostics without echoing proposal values or SQL."""
    text = str(error)
    for marker, code in (
        ("exact public source excerpt", "plan_public_source"),
        ("no public evidence", "plan_filter_source"),
        ("null filtering must be publicly requested", "plan_filter_source"),
        ("join graph", "plan_join_graph"),
        ("unjoined relations", "plan_join_graph"),
        ("outside authorized scope", "plan_field_scope"),
        ("out-of-scope plan field", "plan_field_scope"),
        ("general SQL path", "plan_requires_general_sql"),
        ("ratio scale", "plan_ratio_scale"),
        ("top-k", "plan_top_k"),
        ("tie ordering", "plan_tie_order"),
    ):
        if marker in text:
            return code
    return "invalid_semantic_plan"


def compile_semantic_plan(
    plan: SemanticQueryPlan, *, schema: dict[str, list[str]], question: str, instructions: str = ""
) -> tuple[str, AnalysisTaskSpec]:
    public = question + "\n" + instructions
    if plan.source_excerpt not in public:
        raise ValueError("plan needs an exact public source excerpt")
    if not set(plan.relations) <= schema.keys():
        raise ValueError("plan relation is outside authorized scope")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    def field(reference):
        if (
            reference.relation not in plan.relations
            or reference.column not in schema[reference.relation]
        ):
            raise ValueError("unknown or out-of-scope plan field")
        return quote(reference.relation) + "." + quote(reference.column)

    joined = {plan.relations[0]}
    source = quote(plan.relations[0])
    for join in plan.joins:
        if join.left.relation not in joined or join.right.relation in joined:
            raise ValueError("join graph must be connected and introduce one relation per join")
        left, right = field(join.left), field(join.right)
        source += f" {join.kind.upper()} JOIN {quote(join.right.relation)} ON {left}={right}"
        joined.add(join.right.relation)
    if joined != set(plan.relations):
        raise ValueError("unjoined relations or Cartesian products are not permitted")

    def literal(value, evidence):
        if value is None:
            raise ValueError("null values need explicit is_null/is_not_null operators")
        if type(value) is bool:
            if not re.search(r"\b" + str(value) + r"\b", evidence, re.I):
                raise ValueError("boolean filter value has no public evidence")
            return "1" if value else "0"
        if type(value) in {int, float}:
            public_numbers = set()
            for number in re.findall(
                r"(?<![\w.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
                r"(?!\w|\.\d)",
                evidence,
            ):
                if len(number) > 100:
                    continue
                try:
                    public_numbers.add(Decimal(number))
                except InvalidOperation:
                    continue
            if (
                (type(value) is float and not math.isfinite(value))
                or len(str(value)) > 100
                or Decimal(str(value)) not in public_numbers
            ):
                raise ValueError("numeric filter value has no public evidence")
            return str(value)
        if isinstance(value, str) and len(value) <= 1000:
            evidenced = (
                value.casefold() in evidence.casefold()
                if value
                else bool(re.search(r"''|\"\"", evidence))
            )
            if not evidenced and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                candidates = re.findall(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", evidence)
                for candidate in candidates:
                    try:
                        evidenced |= (
                            date(*map(int, re.split(r"[/-]", candidate))).isoformat() == value
                        )
                    except ValueError:
                        continue
            if not evidenced:
                raise ValueError("text filter value has no public evidence")
            return "'" + value.replace("'", "''") + "'"
        raise ValueError("unsupported plan literal")

    conditions, numerator_conditions, after_conditions, after_fields = [], [], [], []
    operators = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
    predicates = [(predicate, False) for predicate in plan.filters]
    predicates += [(predicate, True) for predicate in plan.numerator_filters]
    for predicate, numerator_only in predicates:
        if predicate.source_excerpt not in public:
            raise ValueError("filter needs an exact public source excerpt")
        column = field(predicate.field)
        target = numerator_conditions if numerator_only else conditions
        if predicate.phase == "after_rank":
            if plan.operation != "rank":
                raise ValueError("after-rank filters require a rank operation")
            after_fields.append(column)
            column = quote("_f" + str(len(after_fields) - 1))
            target = after_conditions
        if predicate.operator in {"is_null", "is_not_null"}:
            if not re.search(r"null|missing|缺失|空值", predicate.source_excerpt, re.I):
                raise ValueError("null filtering must be publicly requested")
            if predicate.value is not None:
                raise ValueError("null filters do not take a value")
            target.append(
                column + (" IS NULL" if predicate.operator == "is_null" else " IS NOT NULL")
            )
        elif predicate.operator == "in":
            if not isinstance(predicate.value, list) or not 1 <= len(predicate.value) <= 100:
                raise ValueError("IN needs one to one hundred public values")
            target.append(
                column
                + " IN ("
                + ",".join(literal(v, predicate.source_excerpt) for v in predicate.value)
                + ")"
            )
        else:
            target.append(
                column
                + operators[predicate.operator]
                + literal(predicate.value, predicate.source_excerpt)
            )
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    keys = [field(key) for key in plan.entity_keys]
    if plan.operation == "count":
        expression = "SELECT * FROM " + source + where
        if plan.count_basis == "entities":
            expression = "SELECT DISTINCT " + ",".join(keys) + " FROM " + source + where
        sql = "SELECT COUNT(*) AS " + quote(plan.count_name) + " FROM (" + expression + ")"
        answer = AnswerSpec(columns=[plan.count_name], shape="scalar", nullable=False)
    elif plan.operation == "population_ratio":
        if not re.search(
            r"\b(?:percentage|proportion|fraction)\s+of\b|\bwhat\s+percent\b", public, re.I
        ):
            raise ValueError(
                "population ratios require an explicit public fraction-of-population request"
            )
        if re.search(
            r"\b(?:growth|change|increase|decrease)\s+rate\b|\b(?:percentage|percent)\s+(?:change|increase|decrease)\b",
            question,
            re.I,
        ):
            raise ValueError("change ratios require the general SQL path")
        if re.search(r"\b(?:SUM|AVG|AVERAGE)\s*\(|\bweighted\b", public, re.I):
            raise ValueError("weighted or measure-based ratios require the general SQL path")
        percent = bool(re.search(r"\bpercent(?:age)?\b", public, re.I))
        if plan.multiplier != (100 if percent else 1):
            raise ValueError("ratio scale differs from the explicit public request")

        def count_population(extra):
            population_where = conditions + extra
            population = (
                "SELECT "
                + ("DISTINCT " + ",".join(keys) if plan.count_basis == "entities" else "*")
                + " FROM "
                + source
            )
            if population_where:
                population += " WHERE " + " AND ".join(population_where)
            return "(SELECT COUNT(*) FROM (" + population + "))"

        numerator, denominator = count_population(numerator_conditions), count_population([])
        expression = f"{plan.multiplier}.0*{numerator}/NULLIF({denominator},0)"
        from generaldataagent.tools.answer_fields import public_precisions

        precision = public_precisions(public)
        if len(precision) > 1:
            raise ValueError("a scalar ratio cannot resolve multiple public rounding requirements")
        if precision:
            digits = next(iter(precision))
            if not 0 <= digits <= 15:
                raise ValueError("public rounding exceeds the bounded precision contract")
            expression = f"ROUND({expression},{digits})"
        sql = "SELECT " + expression + " AS " + quote(plan.ratio_name)
        answer = AnswerSpec(
            columns=[plan.ratio_name],
            shape="scalar",
            minimum=0,
            maximum=plan.multiplier,
            nullable=True,
        )
    else:
        explicit = re.search(r"\btop\s+(\d+)\b", public, re.I)
        if explicit and int(explicit.group(1)) != plan.top_k:
            raise ValueError("top-k differs from the explicit public request")
        if plan.top_k > 1 and not explicit:
            raise ValueError("top-k greater than one requires an explicit public count")
        if plan.ties == "single" and not re.search(
            r"exactly|break\s+ties|neglect.*ties|choose.*first", public, re.I
        ):
            raise ValueError("single-winner tie breaking requires public evidence")
        tie_values = []
        for order in plan.tie_break_by:
            excerpt = order.source_excerpt
            direction_words = (
                r"ascending|smallest|lowest"
                if order.direction == "asc"
                else r"descending|largest|highest"
            )
            if (
                excerpt not in public
                or not re.search(
                    r"(?<!\w)" + re.escape(order.field.column) + r"(?!\w)", excerpt, re.I
                )
                or not re.search(direction_words, excerpt, re.I)
            ):
                raise ValueError("tie ordering needs explicit public field and direction evidence")
            tie_values.append(field(order.field))
        values = [field(p.field) for p in plan.projections]
        partitions = [field(p) for p in plan.partition_by]
        metric = field(plan.metric) if plan.metric else "*"
        if plan.aggregation != "none":
            metric = plan.aggregation.upper() + "(" + metric + ")"
        select = [f"{value} AS {quote('_v' + str(i))}" for i, value in enumerate(values)]
        select += [f"{value} AS {quote('_g' + str(i))}" for i, value in enumerate(partitions)]
        select += [f"{value} AS {quote('_k' + str(i))}" for i, value in enumerate(keys)]
        select += [f"{value} AS {quote('_t' + str(i))}" for i, value in enumerate(tie_values)]
        select += [f"{value} AS {quote('_f' + str(i))}" for i, value in enumerate(after_fields)]
        select.append(metric + " AS " + quote("_metric"))
        base = "SELECT " + ("DISTINCT " if plan.aggregation == "none" else "") + ",".join(select)
        base += " FROM " + source + where
        if plan.aggregation != "none":
            base += " GROUP BY " + ",".join(
                dict.fromkeys(values + partitions + keys + tie_values + after_fields)
            )
        partition = (
            ("PARTITION BY " + ",".join(quote("_g" + str(i)) for i in range(len(partitions))) + " ")
            if partitions
            else ""
        )
        rank = "RANK" if plan.ties == "all" else "ROW_NUMBER"
        tie_break = ""
        if plan.ties == "single":
            tie_break = "," + ",".join(
                quote("_t" + str(i)) + " " + order.direction.upper()
                for i, order in enumerate(plan.tie_break_by)
            )
        ranked = (
            "SELECT *,"
            + rank
            + "() OVER ("
            + partition
            + "ORDER BY "
            + quote("_metric")
            + " "
            + plan.direction.upper()
            + tie_break
            + ") AS "
            + quote("_rank")
            + " FROM ("
            + base
            + ")"
        )
        projection = ",".join(
            quote("_v" + str(i)) + " AS " + quote(p.name) for i, p in enumerate(plan.projections)
        )
        sql = (
            "SELECT "
            + projection
            + " FROM ("
            + ranked
            + ") WHERE "
            + quote("_rank")
            + " <= "
            + str(plan.top_k)
            + (" AND " + " AND ".join(after_conditions) if after_conditions else "")
        )
        answer = AnswerSpec(columns=[p.name for p in plan.projections], ties=plan.ties)
    SqlGuard(dialect="sqlite").inspect(sql, allowed_relations=set(plan.relations))
    spec = AnalysisTaskSpec(
        question=question,
        public_requirements=instructions,
        grain=[key.relation + "." + key.column for key in plan.entity_keys],
        filters=conditions + after_conditions,
        count_basis=plan.count_basis
        if plan.operation in {"count", "population_ratio"}
        else "unspecified",
        answer=answer,
    )
    return sql, spec


def physical_plan_schema(database, *, relations, allowed_relations):
    """Read exact authorized physical field names; no sampled schema inference."""
    import sqlite3

    if not set(relations) <= set(allowed_relations):
        raise ValueError("plan relation is outside authorized scope")
    schema = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for table in relations:
            quoted = '"' + table.replace('"', '""') + '"'
            schema[table] = [row[1] for row in db.execute(f"PRAGMA table_info({quoted})")]
    return schema


def execute_semantic_plan(
    database, plan, *, allowed_relations, question, instructions="", max_rows=200
):
    """Compile against physical scoped schema and execute a complete bounded read-only result.

    Compilation and execution remain separate from
    independent business interpretation review and benchmark scoring.
    """
    from generaldataagent.tools.sql_semantics import (
        SemanticError,
        check_percentage_result,
        inspect_semantics,
        validate_column_scope,
        validate_rows,
    )
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        raise ValueError("plan result limit must be between one and ten thousand")
    schema = physical_plan_schema(
        database, relations=plan.relations, allowed_relations=allowed_relations
    )
    sql, spec = compile_semantic_plan(
        plan, schema=schema, question=question, instructions=instructions
    )
    validate_column_scope(database, sql, allowed_relations=set(allowed_relations))
    checks = inspect_semantics(sql, spec, schema=schema)
    if checks:
        raise SemanticError(",".join(checks))
    result = SQLiteReadOnlyExecutor(
        database, max_rows=max_rows, max_cells=10000, timeout_seconds=5
    ).execute(sql)
    checks = validate_rows(result.columns, result.rows, spec.answer)
    checks.extend(check_percentage_result(sql, question, instructions, result.rows))
    if checks:
        raise SemanticError(",".join(checks))
    return {
        "sql": sql,
        "task_contract": spec.model_dump(),
        **result.as_dict(),
        "complete": True,
        "method_id": "semantic_plan",
        "business_semantics_verified": False,
    }
