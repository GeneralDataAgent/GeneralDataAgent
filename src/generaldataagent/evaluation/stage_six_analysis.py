"""Fixed, read-only analysis and bounded document retrieval for evaluation tasks.

No generated Python, imports, shell commands, files or database writes are accepted.
"""

import hashlib
import math
import re
import statistics

from generaldataagent.contracts.analysis import AnalysisArguments
from generaldataagent.tools.analysis_scope import validate_analysis_columns
from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor


def retrieve_documents(documents: str, query: str, *, max_chars: int = 6000) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 300 or max_chars < 100:
        raise ValueError("invalid document query")
    terms = set(re.findall(r"[a-z0-9_]+", query.casefold()))
    chunks = []
    for section in re.split(r"(?m)(?=^#{1,4} )", documents):
        for offset in range(0, len(section), 1800):
            text = section[max(0, offset - 150) : offset + 2000]
            words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
            score = len(terms & words)
            if score:
                chunks.append((score, text))
    chunks.sort(key=lambda item: (-item[0], hashlib.sha256(item[1].encode()).hexdigest()))
    results = []
    remaining = max_chars
    for _, text in chunks[:4]:
        excerpt = text[:remaining]
        if not excerpt:
            break
        results.append(
            {"chunk_id": hashlib.sha256(text.encode()).hexdigest(), "untrusted_excerpt": excerpt}
        )
        remaining -= len(excerpt)
    return {"query": query, "matches": results, "truncated": len(chunks) > len(results)}


def document_outline(documents: str) -> str:
    return "\n".join(line for line in documents.splitlines() if line.startswith("#"))[:3000]


def fixed_analysis(
    database, specification: dict, *, allowed_relations: set[str], public_text: str = ""
) -> dict:
    arguments = AnalysisArguments.model_validate(specification)
    if arguments.operation == "independent_t_test":
        from generaldataagent.tools.independent_test import independent_test

        return independent_test(
            database, arguments, allowed_relations=allowed_relations, public_text=public_text
        )
    table = specification["table"]
    columns = specification["columns"]
    operation = specification["operation"]
    if table not in allowed_relations:
        raise ValueError("analysis relation is outside task scope")
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= 2
        or not all(isinstance(c, str) and len(c) <= 200 for c in columns)
    ):
        raise ValueError("one or two explicit columns required")
    if operation not in {"describe", "pearson", "pearson_test", "linear_regression"}:
        raise ValueError("unsupported fixed analysis")
    if operation != "describe" and len(columns) != 2:
        raise ValueError("paired analysis requires two columns")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    # Verify physical column names: SQLite double-quoted-string compatibility must not
    # turn a misspelled identifier into an apparently valid constant numeric feature.
    validate_analysis_columns(
        database,
        table,
        columns + [f.column for f in arguments.filters],
        allowed_relations=allowed_relations,
    )
    predicates, parameters = [], []
    for item in arguments.filters:
        if item.source_excerpt not in public_text or not re.search(
            r"(?<!\w)" + re.escape(item.value) + r"(?!\w)", item.source_excerpt, re.I
        ):
            raise ValueError("describe filter requires exact public source and value")
        predicates.append(
            quote(item.column) + "=?"
            if item.operator == "eq"
            else "instr(" + quote(item.column) + ",?)>0"
        )
        parameters.append(item.value)
    executor = SQLiteReadOnlyExecutor(database, max_rows=200_000, timeout_seconds=5)
    sql = (
        "SELECT "
        + ",".join(quote(c) for c in columns)
        + " FROM "
        + quote(table)
        + (" WHERE " + " AND ".join(predicates) if predicates else "")
    )
    result = executor.execute(sql, tuple(parameters))
    rows = [
        row
        for row in result.rows
        if all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in row
        )
    ]
    output = {
        "operation": operation,
        "columns": columns,
        "observations": len(rows),
        "dropped_rows": len(result.rows) - len(rows),
        "method": "pairwise finite values",
    }
    if operation == "describe":
        from generaldataagent.tools.iqr import iqr_summary, public_iqr_conflicts

        summaries = {}
        iqr_statistics = {}
        for index, column in enumerate(columns):
            values = [
                row[index]
                for row in result.rows
                if isinstance(row[index], (int, float))
                and not isinstance(row[index], bool)
                and math.isfinite(row[index])
            ]
            summaries[column] = {
                "count": len(values),
                "sum": math.fsum(values),
                "mean": statistics.mean(values) if values else None,
                "median": statistics.median(values) if values else None,
                "sample_std": statistics.stdev(values) if len(values) > 1 else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
            }
            iqr_statistics[column] = iqr_summary(values)
        if not any(summary["count"] for summary in summaries.values()):
            raise ValueError("no finite observations")
        output.update(
            statistics=summaries,
            method="columnwise finite values",
            observations=len(result.rows),
            dropped_rows=None,
            iqr_statistics=iqr_statistics,
            iqr_method="linear inclusive quantiles h=(n-1)*p; strict 1.5-IQR fences",
            iqr_scope=(
                "same columnwise finite population; quantile convention not publicly verified"
            ),
            iqr_public_conflicts=public_iqr_conflicts(public_text),
            population_filters=[
                {"column": f.column, "value": f.value, "operator": f.operator}
                for f in arguments.filters
            ],
            population_scope=(
                "public text filters; eq or literal case-sensitive contains; "
                "business interpretation unverified"
                if arguments.filters
                else "full physical table"
            ),
        )
        from generaldataagent.tools.analysis_methods import requests_python_statistics

        if requests_python_statistics(public_text):
            output["python_statistics_implementation"] = {
                "mean": "statistics.mean",
                "median": "statistics.median",
            }
    elif operation == "pearson":
        if not rows:
            raise ValueError("no finite observations")
        output["correlation"] = statistics.correlation([r[0] for r in rows], [r[1] for r in rows])
    elif operation == "pearson_test":
        from generaldataagent.tools.pearson_test import pearson_test

        output.update(
            pearson_test(
                rows,
                rule=arguments.decision_rule,
                significance_rule=arguments.significance_rule,
                public_text=public_text,
            )
        )
    else:
        fit = statistics.linear_regression([r[0] for r in rows], [r[1] for r in rows])
        output.update(
            slope=fit.slope,
            intercept=fit.intercept,
            scope="descriptive full-snapshot fit; not held-out predictive evaluation",
        )
    return output


def normalize_visible_answers(task, answers: dict) -> dict:
    """Parse the task's public closed-form notation; never inspect golden label keys."""
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object")
    names = set(re.findall(r"@(\w+)\[", task.instructions))
    if names and set(answers) == {"answer"} and isinstance(answers["answer"], str):
        extracted = re.findall(r"@(\w+)\[([^\[\]]*)\]", answers["answer"])
        if extracted and len({key for key, _ in extracted}) == len(extracted):
            parsed = dict(extracted)
            if set(parsed) == names:
                return parsed
    return answers
