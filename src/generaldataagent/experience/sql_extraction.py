"""Answer-blind SQL repair candidates from adjacent, context-bound tool observations."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from generaldataagent.contracts.experience import (
    ExperienceRecord,
    ExtractedExperience,
    SqlRepairEvidence,
)
from generaldataagent.tools.sql_guard import SqlGuard, SqlGuardError

SQL_REPAIR_TEMPLATE = "sql-repair-v1"
SQL_REPAIR_GUIDANCE = {
    "unknown_column": (
        "a read-only query reports an unresolved column",
        "Inspect the visible schema and aliases; replace the unresolved column with an "
        "authorized field whose documented meaning fits the requested computation.",
    ),
    "ambiguous_column": (
        "a read-only query reports an ambiguous column",
        "Qualify the ambiguous field using the intended visible relation or alias; "
        "check that the join and requested population are preserved.",
    ),
    "sql_syntax": (
        "a read-only query is rejected for SQL syntax",
        "Correct the query syntax for the active dialect; retain the requested "
        "population, calculations and read-only relation scope.",
    ),
    "unsupported_function": (
        "a read-only query calls a function unavailable in the active database",
        "Check the active dialect's supported functions; use an authorized supported "
        "expression only after checking that its semantics fit the requested computation.",
    ),
    "function_arguments": (
        "a read-only query reports an invalid function argument count",
        "Check the supported function signature and correct its arguments; verify "
        "types, null behavior and the requested calculation before reusing the result.",
    ),
}


def sql_repair_context_hash(context: dict) -> str:
    """Bind declared task/schema/profile/action context; not database contents or identity."""
    raw = json.dumps(context, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def observe_sql_repair(failure: dict, success: dict) -> SqlRepairEvidence | None:
    """Bind adjacent observations; do not execute queries or infer answer correctness."""
    if (
        failure.get("status") != "error"
        or failure.get("tool") != "sql"
        or not isinstance(failure.get("code"), str)
        or failure.get("code") not in SQL_REPAIR_GUIDANCE
        or failure.get("error") not in ("SqlGuardError", "QueryExecutionError")
        or success.get("status") != "ok"
        or success.get("tool") != "sql"
    ):
        return None
    start, end = failure.get("step"), success.get("step")
    if type(start) is not int or type(end) is not int or start < 1 or end != start + 1:
        return None
    context = failure.get("sql_repair_context_hash")
    digest = success.get("result_hash")
    if (
        not isinstance(context, str)
        or not re.fullmatch(r"[a-f0-9]{64}", context)
        or context != success.get("sql_repair_context_hash")
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        return None
    before, after = failure.get("attempted_sql"), success.get("sql")
    if (
        not isinstance(before, str)
        or not isinstance(after, str)
        or not 1 <= len(before) <= 10000
        or not 1 <= len(after) <= 10000
        or before.strip() == after.strip()
        or type(success.get("row_count")) is not int
        or success["row_count"] < 0
    ):
        return None
    try:
        normalized_after = SqlGuard(dialect="sqlite").inspect(after).normalized_sql
    except SqlGuardError:
        return None
    try:
        if SqlGuard(dialect="sqlite").inspect(before).normalized_sql == normalized_after:
            return None
    except SqlGuardError as exc:
        if failure["code"] != "sql_syntax" or not str(exc).startswith("SQL parsing failed:"):
            return None
    return SqlRepairEvidence(
        context_hash=context,
        failure_code=failure["code"],
        failure_step=start,
        recovery_step=end,
        failed_query_hash=hashlib.sha256(before.encode()).hexdigest(),
        recovered_query_hash=hashlib.sha256(after.encode()).hexdigest(),
        result_hash=digest,
    )


def extract_sql_experiences(
    trace: list[dict], *, owner: str, created_at: datetime
) -> tuple[ExtractedExperience, ...]:
    """At most one fixed lesson per supported SQL failure class, all awaiting review.

    Trace SQL, names, literals, error prose and results never enter lesson text.
    A changed query followed by tool acceptance is an observation, not causal or
    semantic verification. Unknown, old, interleaved and unbound traces are skipped.
    """
    candidates = []
    seen = set()
    for failure, success in zip(trace, trace[1:], strict=False):
        if not isinstance(failure, dict) or not isinstance(success, dict):
            continue
        receipt = observe_sql_repair(failure, success)
        if (
            receipt is None
            or receipt.failure_code in seen
            or success.get("sql_repair_evidence") != receipt.model_dump(mode="json")
        ):
            continue
        condition, repair = SQL_REPAIR_GUIDANCE[receipt.failure_code]
        record = ExperienceRecord(
            failure_class=receipt.failure_code,
            applies_when=(condition,),
            does_not_apply_when=(
                "the declared task, visible schema, dialect or authorization scope has changed",
                "execution succeeds but the intended meaning or output completeness is unverified",
            ),
            repair=repair + " Retry only within the existing repair budget.",
            counterexample=(
                "A different query can execute while answering a different question. "
                "Tool acceptance alone does not validate semantics, complete coverage or "
                "permission to read another relation; independent review is still required."
            ),
            tool_scope=("sql",),
            owner=owner,
            created_at=created_at,
        )
        candidates.append(
            ExtractedExperience(
                template_version=SQL_REPAIR_TEMPLATE,
                failure_step=receipt.failure_step,
                recovery_step=receipt.recovery_step,
                result_hash=receipt.result_hash,
                record=record,
            )
        )
        seen.add(receipt.failure_code)
    return tuple(candidates)
