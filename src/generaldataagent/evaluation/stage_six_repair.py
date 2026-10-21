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
