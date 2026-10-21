"""Read-only two-group Student/Welch test with explicit finite-sample p-values."""

import hashlib
import math
import re
import statistics
import warnings
from decimal import Decimal

from generaldataagent.catalog.field_evidence import public_quoted_labels
from generaldataagent.tools.analysis_scope import validate_analysis_columns
from generaldataagent.tools.sql_guard import SqlGuard
from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor


def requests_independent_test(public_text: str) -> bool:
    return bool(
        re.search(
            r"\bindependent[\s-]+(?:(?:two[\s-]+)?samples?[\s-]+)?t[\s-]?test\b", public_text, re.I
        )
    )


def independent_method_bindings(bindings: dict, evidence: dict, public_text: str) -> bool:
    if not requests_independent_test(public_text):
        return True
    return bool(bindings) and all(
        evidence.get(step, {}).get("method_id") == "independent_t_test"
        for step, _path in bindings.values()
    )


def independent_test(database, arguments, *, allowed_relations: set[str], public_text: str) -> dict:
    import scipy
    from scipy.stats import ttest_ind

    spec = arguments.test
    named_welch = bool(re.search(r"\bwelch\b", public_text, re.I))
    named_student = bool(re.search(r"\bstudent(?:'s)?\s+t[\s-]?test\b", public_text, re.I))
    if named_welch != named_student and spec.variance != ("welch" if named_welch else "student"):
        raise ValueError("variance method conflicts with the public named test")
    table = arguments.table
    if table not in allowed_relations:
        raise ValueError("analysis relation is outside task scope")

    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    validate_analysis_columns(
        database,
        table,
        arguments.columns + [f.column for f in spec.filters],
        allowed_relations=allowed_relations,
    )
    measure, group = arguments.columns
    predicates = [quote(group) + " IN (?,?)"]
    parameters = list(spec.groups)
    for item in spec.filters:
        if item.source_excerpt not in public_text or not re.search(
            r"(?<!\w)" + re.escape(item.value) + r"(?!\w)", item.source_excerpt, re.I
        ):
            raise ValueError("group filter requires exact public source and value")
        predicates.append(quote(item.column) + "=?")
        parameters.append(item.value)
    sql = (
        "SELECT "
        + quote(measure)
        + ","
        + quote(group)
        + " FROM "
        + quote(table)
        + " WHERE "
        + " AND ".join(predicates)
    )
    SqlGuard(dialect="sqlite").inspect(sql, allowed_relations=allowed_relations)
    result = SQLiteReadOnlyExecutor(database, max_rows=200_000, timeout_seconds=5).execute(
        sql, tuple(parameters)
    )
    values = [
        [
            row[0]
            for row in result.rows
            if row[1] == label
            and isinstance(row[0], (int, float))
            and not isinstance(row[0], bool)
            and math.isfinite(row[0])
        ]
        for label in spec.groups
    ]
    if any(len(group_values) < 2 for group_values in values):
        raise ValueError("independent test requires at least two finite observations per group")
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        try:
            fit = ttest_ind(
                *values,
                equal_var=spec.variance == "student",
                nan_policy="raise",
                alternative="two-sided",
            )
        except RuntimeWarning as exc:
            raise ValueError("numerically unstable independent test") from exc
    statistic, p_value, df = float(fit.statistic), float(fit.pvalue), float(fit.df)
    if not all(math.isfinite(v) for v in (statistic, p_value, df)) or df <= 0:
        raise ValueError("undefined independent test result")
    output = {
        "operation": "independent_t_test",
        "columns": arguments.columns,
        "variance": spec.variance,
        "equal_population_variances_assumed": spec.variance == "student",
        "alternative": "two-sided",
        "statistic": statistic,
        "p_value": p_value,
        "degrees_of_freedom": df,
        "observations": sum(map(len, values)),
        "dropped_rows": len(result.rows) - sum(map(len, values)),
        "filters": [item.model_dump() for item in spec.filters],
        "groups": [
            {
                "value": label,
                "count": len(group_values),
                "mean": statistics.mean(group_values),
                "sample_std": statistics.stdev(group_values),
                "population_std": statistics.pstdev(group_values),
            }
            for label, group_values in zip(spec.groups, values, strict=True)
        ],
        "test_method": "scipy.stats.ttest_ind theoretical finite-sample t distribution",
        "scipy_version": scipy.__version__,
        "assumptions_verified": False,
        "scope": "declared groups/filters; independence, normality and group meanings unverified",
    }
    if spec.significance_rule is not None:
        rule = spec.significance_rule
        numbers = {
            Decimal(value)
            for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w|\.\d)", public_text)
        }
        if Decimal(str(rule.alpha)) not in numbers or not {
            rule.significant_label,
            rule.nonsignificant_label,
        } <= set(public_quoted_labels(public_text)):
            raise ValueError("significance rule requires original public threshold and labels")
        named_levels = re.findall(
            r"\b(?:alpha|significance\s+level)\)?\s*(?:of|=|:)?\s*"
            r"(\d+(?:\.\d+)?)(?!\w|\.\d)",
            public_text,
            re.I,
        )
        if named_levels and {Decimal(v) for v in named_levels} != {Decimal(str(rule.alpha))}:
            raise ValueError("significance rule conflicts with the public named level")
        output.update(
            public_rule_label=rule.significant_label
            if p_value < rule.alpha
            else rule.nonsignificant_label,
            significance_rule=rule.model_dump(),
            policy_interpretation_verified=False,
            decision_source_sha256=hashlib.sha256(public_text.encode()).hexdigest(),
        )
    return output
