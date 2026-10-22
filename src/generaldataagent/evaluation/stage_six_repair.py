"""Answer-blind action repair and bounded observations; never supplies benchmark labels."""

import json
import re

REPAIR_PROMPT = """
Plan before querying. Identify requested filters, units, denominator, join keys and output fields.
Only one action per turn. Do not attach answers to SQL; first observe execution, then answer.
For BIRD, prefer final_sql once you have the requested query; no explanatory prose is required.
After a successful aggregate/statistic, finish instead of repeating it. An empty result needs
checking actual category/date values, not repeating the same filter. Use exact existing columns.
SQLite has no MEDIAN, STDDEV or CORR aggregate: use fixed analysis for median/std/Pearson.
For fee JSON inspect a small targeted sample and specific json_extract fields, never all payloads.
Text previews may be truncated; compute answers with full-data queries, never from preview samples.
Use the requested rounding, include every visible answer field, and cite observed successful steps.
"""


def repair_action(action: dict) -> tuple[dict, str | None]:
    if not isinstance(action, dict):
        raise ValueError("one JSON object required")
    keys = {"sql", "final_sql", "answers", "analysis", "search", "unsupported"}
    present = {key for key in keys & action.keys() if action[key] not in (None, "")}
    cleaned = {key: value for key, value in action.items() if key not in keys or key in present}
    if len(present) == 1:
        return cleaned, "empty_action_fields_removed" if cleaned != action else None
    # Execute the request; a model-written answer cannot replace the observation.
    if present == {"sql", "answers"}:
        return {"sql": action["sql"]}, "premature_answers_discarded"
    return cleaned, None


def bounded_observation(result: dict, *, max_chars: int = 6000) -> dict:
    if max_chars < 1000:
        raise ValueError("observation budget too small")
    serialized = json.dumps(result, default=str)
    if len(serialized) <= max_chars:
        return result
    if "rows" not in result:
        output = {
            "truncated": True,
            "untrusted_preview": serialized[: max_chars - 350],
            "hint": "Retrieve specific fields or narrower document terms; preview is incomplete.",
        }
        while len(json.dumps(output)) > max_chars:
            output["untrusted_preview"] = output["untrusted_preview"][:-200]
        return output
    output = {
        "columns": result["columns"],
        "rows": [],
        "total_rows": len(result["rows"]),
        "truncated": True,
        "hint": "Preview only. Query full-data aggregates or narrower columns/filters for answers.",
    }
    # Budget column metadata too, even for exceptionally wide relations.
    if len(json.dumps(output, default=str)) > max_chars - 500:
        output["columns"] = []
        output["columns_truncated"] = True
        for column in list(result["columns"])[:20]:
            output["columns"].append(str(column)[:120])
            if len(json.dumps(output)) > max_chars - 500:
                output["columns"].pop()
                break
        return output
    for row in result["rows"]:
        preview = [
            v[:240] + "…[truncated]" if isinstance(v, str) and len(v) > 240 else v for v in row
        ]
        output["rows"].append(preview)
        if len(json.dumps(output, default=str)) > max_chars:
            output["rows"].pop()
            break
    return output


def execution_hint(exc: Exception) -> str:
    text = str(exc)
    if "iqr_public_method_conflict" in text:
        return (
            "describe uses linear inclusive quartiles and strict 1.5-IQR fences. The public "
            "text explicitly requests another recognized convention. Compute that exact method "
            "with supported scoped SQL, or declare unsupported; do not bind these IQR values."
        )
    if "public_extremum_representation" in text:
        return (
            "The public text explicitly specifies native MIN/MAX of a physical field. "
            "Preserve that expression and its returned type; an added CAST changes the "
            "declared formula or representation. Recheck the requested population and ties."
        )
    if "public_year_formula_fields" in text:
        return (
            "Use the physical source fields in the explicit public year-difference formula, "
            "in its stated order. Another event date cannot replace a declared source date. "
            "Preserve the requested outputs and verify actual date encoding."
        )
    if "independent_test_requires_method_bindings" in text:
        return (
            "This public task explicitly requests an independent sample t-test. Use fixed_analysis "
            "operation independent_t_test, then bind every requested field to that result's "
            "groups, p_value or public_rule_label. A SQL CASE with 1.96, an unfiltered Pearson "
            "test or data-dependent descriptive SQL is not a receipt for the requested test."
        )
    if (
        "mixed_sql_requires_remaining_scalar_aliases" in text
        or "mixed_sql_requires_one_scalar_row" in text
    ):
        return (
            "Mixed final SQL must return exactly one row, aliased only to the remaining scalar "
            "public keys. Bind every collection separately through collections name/step; "
            "do not serialize arrays or include collection columns in this SQL."
        )
    if "mixed_sql_requires_prior_complete_collection" in text:
        return (
            "Select a prior successful collect_values step. SQL exploration rows and previews "
            "are not complete collection evidence, and current/future steps cannot be bound."
        )
    if "plan_public_source" in text or "plan_filter_source" in text:
        return (
            "Omit top-level plan source_excerpt: runtime binds the original public question. "
            "Each filter source_excerpt must be copied exactly from question or instructions, "
            "without Question prefixes, schema facts, explanations or paraphrases. "
            "Its literal must occur in that public excerpt; do not invent a new value."
        )
    if "plan_requires_general_sql" in text:
        return "This plan cannot express the formula; use submit_sql with scoped read-only SQL."
    if "plan_join_graph" in text:
        return (
            "Every listed relation must occur in one connected join graph. Each join introduces "
            "one new listed relation; use physical scoped fields, not invented aliases."
        )
    if "plan_ratio_scale" in text:
        return "Use multiplier 100 for the public percentage, or 1 for a fraction/proportion."
    if "answer_sql_requires_scalar_public_fields" in text:
        return (
            "This public task contains a collection. Use collect_values for its full items, "
            "compute_sql or fixed_analysis for the remaining scalar fields, then submit_answers "
            "binding every key to those successful steps. submit_sql_answers is scalar-only."
        )
    if "public_age_order_direction" in text:
        return (
            "The public instructions explicitly relate this field's value to age. "
            "Choose the minimum or maximum in the direction required for oldest/youngest; "
            "preserve the requested population and every requested output."
        )
    if any(
        code in text
        for code in (
            "change_real_division_unproven",
            "change_direction",
            "change_reference_period",
            "change_percent_scale",
        )
    ):
        hints = []
        if "change_real_division_unproven" in text:
            hints.append(
                "Promote a numerator or denominator to REAL before each growth-rate division, "
                "e.g. 1.0*(end-start)/NULLIF(start,0). A decimal multiplier or cast after division "
                "cannot restore discarded fractional digits."
            )
        if "change_direction" in text or "change_reference_period" in text:
            hints.append(
                "For the public change subtract the starting value from the ending value and "
                "divide by the starting value; aliases alone cannot repair the formula."
            )
        if "change_percent_scale" in text:
            hints.append("The public output is a percentage: multiply the change fraction by 100.")
        return " ".join(hints) + (
            " Fix all reported checks together in one revised query. Preserve periods, filters "
            "and lookup selection."
        )
    if "cell limit" in text:
        return (
            "The complete final/check result exceeds the bounded verification cell budget. "
            "Do not add LIMIT, drop requested columns, or change filters merely to fit. "
            "Declare unsupported if the requested full result cannot be certified."
        )
    if "contradicts the explicit public format" in text:
        return (
            "Preserve the runtime's explicit public field type and nullability. Omit optional "
            "rule overrides instead of changing a collection to a string or changing scalar types."
        )
    if "data_dependent_answer_required" in text:
        return (
            "Bind a computed scalar projection, not constants or whole SQL row containers. "
            "For a complete collection use collect_values with one DISTINCT data-derived "
            "column, then bind path [items]. Column-name labels must be conditional on data "
            "for that exact physical field. For missing-column names, a supported form is "
            "SELECT DISTINCT 'field_name' AS name FROM scoped_table WHERE field_name IS NULL. "
            "Replace both field_name occurrences with the same actual physical column. "
            "Add only publicly requested missing markers and column-type restrictions; "
            "combine eligible branches with UNION and order the full collection as requested."
        )
    if "field requires complete collection evidence" in text:
        return (
            "Use collect_values with one full-data output column and no LIMIT/OFFSET, "
            "then submit_answers binding path [items]. For a list use collection_kind=list "
            "to preserve duplicates; sets require distinct values. Preserve public ordering. "
            "A scalar or serialized string is not a verified complete collection."
        )
    if any(code in text for code in ("predicate_scope", "date_scope", "ratio_scope")):
        return (
            "A requirement scope must identify root, a unique CTE/subquery alias, or a unique "
            "direct physical source of the root SELECT. Nested table names and ambiguous "
            "self-joins cannot identify a scope. Preserve the accepted requirement."
        )
    if "data_dependent_sql_required" in text:
        return (
            "Every final SQL output must depend on scoped table data. Use explicit data-derived "
            "projections; constant answers and constants wrapped in a CTE cannot be certified."
        )
    if "unrequested_all_count_null_filter" in text:
        return (
            "The public formula counts all objects. Excluding missing sum-field values also "
            "changes that denominator population. Preserve the requested all-object count."
        )
    if "percentage_requires_ratio_not_counts" in text:
        return (
            "The question asks for a percentage; raw population counts are only intermediate "
            "evidence. Submit the requested ratio using the public numerator and denominator "
            "populations, real division, and the requested scale."
        )
    if "undefined_percentage_check_population" in text:
        return (
            "The percentage is undefined for this population. Check the table/field and actual "
            "public-literal matches before changing SQL. Do not merely add NULLIF or COALESCE, "
            "and do not add optional requirements to repair a population error."
        )
    if "ratio_zero_guard" in text:
        return "Use NULLIF(denominator,0) without changing the declared population or formula."
    # Only known local SQL diagnostics; never echo arbitrary model/SDK exceptions.
    if "wrong number of arguments to function" in text:
        return (
            "The SQL function has the wrong argument count. Check its SQLite signature "
            "and provide the required arguments; keep the requested computation."
        )
    for marker in (
        "no such column:",
        "no such table:",
        "no such function:",
        "ambiguous column name:",
    ):
        if marker in text:
            identifier = text.split(marker, 1)[1].strip()
            if re.fullmatch(r'[\w ."`-]{1,120}', identifier):
                return marker + " " + identifier + ". Check schema or use a fixed analysis tool."
    if "row limit" in text:
        return (
            "Too many result rows. Aggregate in SQL, or select fewer rows/fields for exploration."
        )
    if "unreviewed SQL functions" in text:
        return "Function not allowed. Use documented SQLite functions or the fixed analysis tool."
    return "Revise the action using the visible schema and supported tools."
