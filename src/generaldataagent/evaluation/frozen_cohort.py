"""Freeze an unused evaluation cohort before it is run, never after.

Stage 10's exit criterion calls for tasks and assets that were not used during
development. A sampled cohort is only unseen if the sample is declared first:
choosing it after seeing scores turns every result into a post-hoc pick. This
module writes the declaration from prior reports, and the runner refuses a frozen
file whose IDs or task content no longer match.

The file stores case IDs and a hash of the exact ordered tasks. It deliberately
stores no question text, reference SQL or answer: a frozen cohort handed to a
reviewer must not become a label leak.
"""

import hashlib
import json

from generaldataagent.evaluation.stage_six import Case, cohort_hash, sample_cases
from generaldataagent.evaluation.stage_six_stability import canonical_report_hash


def seen_case_ids(reports: list[dict], benchmark: str) -> set[str]:
    """Every case ID that any prior report for this benchmark already ran."""
    seen = set()
    for report in reports:
        if report.get("benchmark") != benchmark:
            raise ValueError("prior report belongs to another benchmark")
        if not isinstance(report.get("results"), list):
            raise ValueError("prior report has no case results")
        seen.update(str(row["case_id"]) for row in report["results"])
    return seen


def freeze_cohort(
    cases: list[Case], *, benchmark: str, limit: int, seed: int, reports: list[dict]
) -> dict:
    """Declare the next cohort from cases no prior report has run."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not reports:
        raise ValueError("at least one prior report is required to know what is used")
    if not cases:
        raise ValueError("no cases available")
    if {case.task.benchmark for case in cases} != {benchmark}:
        raise ValueError("case set and benchmark disagree")
    seen = seen_case_ids(reports, benchmark)
    available = {case.task.case_id: case for case in cases}
    excluded = sorted(set(available) & seen)
    unseen = [available[key] for key in sorted(set(available) - seen)]
    if len(unseen) < limit:
        raise ValueError(f"only {len(unseen)} unseen cases remain; cannot freeze {limit}")
    selected = sample_cases(unseen, limit, seed)
    freeze = {
        "benchmark": benchmark,
        "seed": seed,
        "limit": limit,
        "excluded_seen": len(excluded),
        "available_unseen": len(unseen),
        "case_ids": [case.task.case_id for case in selected],
        "cohort_hash": cohort_hash(selected),
        "prior_reports": {
            "cohort_hashes": sorted({report["cohort_hash"] for report in reports}),
            "report_hashes": sorted({canonical_report_hash(report) for report in reports}),
        },
        "declaration": (
            "IDs only; no question text, reference SQL or answers. The hash covers the "
            "exact ordered tasks, so a changed source fails the runner's check."
        ),
    }
    return freeze


def frozen_file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_frozen_cases(cases: list[Case], cohort: dict) -> list[Case]:
    """Select exactly the declared cohort, in its declared order, or refuse."""
    if not cases:
        raise ValueError("no cases available")
    if cohort.get("benchmark") != cases[0].task.benchmark:
        raise ValueError("frozen cohort benchmark mismatch")
    ids = cohort.get("case_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, str) or not item for item in ids)
    ):
        raise ValueError("frozen cohort needs a non-empty case_id list")
    if len(set(ids)) != len(ids):
        raise ValueError("frozen cohort has duplicate case IDs")
    by_id = {case.task.case_id: case for case in cases}
    missing = [item for item in ids if item not in by_id]
    if missing:
        raise ValueError("frozen cohort names cases that do not exist: " + ",".join(missing[:5]))
    selected = [by_id[item] for item in ids]
    declared = cohort.get("cohort_hash")
    if not isinstance(declared, str) or not declared:
        raise ValueError("frozen cohort must declare cohort_hash")
    if declared != cohort_hash(selected):
        raise ValueError("frozen cohort does not match the current task set")
    return selected


def load_frozen_cohort(path) -> dict:
    """Read a freeze file, rejecting anything that is not a JSON object."""
    try:
        cohort = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError("frozen cohort is not valid JSON") from exc
    if not isinstance(cohort, dict):
        raise ValueError("frozen cohort must be a JSON object")
    return cohort
