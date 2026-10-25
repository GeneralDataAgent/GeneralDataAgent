"""Explicit experience on/off diagnostics; never relax the general pairing gate."""

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy

from generaldataagent.evaluation.stage_six_comparison import SERVICE_FAILURES
from generaldataagent.evaluation.stage_six_gates import validate_report
from generaldataagent.evaluation.stage_six_stability import canonical_report_hash

_TREATMENT = {"experience_reuse", "experience_store_hash", "experience_scope_hash"}
_HINT_FIELDS = {"step", "failure_code", "tool", "n", "dropped"}


def _delivered_hints(rows: list[dict]) -> dict:
    """Count per-case hint delivery; the on arm must show the treatment arriving."""
    by_code: Counter = Counter()
    cases = hints = dropped = 0
    for row in rows:
        if "experience_used" not in row:
            raise ValueError("experience delivery evidence missing")
        used = row["experience_used"]
        if not isinstance(used, list):
            raise ValueError("experience delivery evidence invalid")
        if any(
            isinstance(hint, dict) and type(hint.get("n")) is int and hint["n"] > 0 for hint in used
        ):
            cases += 1
        unavailable_steps = {
            event["step"] for event in row["trace"] if event["status"] == "experience_unavailable"
        }
        previous_step = 0
        for hint in used:
            if (
                not isinstance(hint, dict)
                or set(hint) != _HINT_FIELDS
                or type(hint["step"]) is not int
                or hint["step"] < 1
                or not isinstance(hint["failure_code"], str)
                or not hint["failure_code"].strip()
                or not isinstance(hint["tool"], str)
                or type(hint["n"]) is not int
                or hint["n"] < 0
                or type(hint["dropped"]) is not int
                or hint["dropped"] < 0
                or hint["n"] == hint["dropped"] == 0
            ):
                raise ValueError("experience delivery evidence invalid")
            # The worker emits at most one entry per recovery step. Repeated
            # or reordered entries must not inflate delivery or drop counts.
            if hint["step"] <= previous_step:
                raise ValueError("experience delivery steps must strictly increase")
            previous_step = hint["step"]
            # A failed lookup emits no hints or budget counts for that call.
            if hint["step"] in unavailable_steps:
                raise ValueError("experience delivery conflicts with unavailable step")
            if hint["n"]:
                by_code[hint["failure_code"]] += hint["n"]
            hints += hint["n"]
            dropped += hint["dropped"]
    return {
        "cases_with_hints": cases,
        "hints": hints,
        "dropped": dropped,
        "by_failure_code": dict(sorted(by_code.items())),
    }


def _unavailable_steps(rows: list[dict]) -> list[int]:
    """Steps whose trace recorded an advisory-port outage.

    A missing or malformed trace is refused rather than read as healthy: absence
    of evidence must not clear an attribution blocker.
    """
    steps = []
    for row in rows:
        trace = row.get("trace")
        if not isinstance(trace, list) or any(
            not isinstance(event, dict)
            or type(event.get("step")) is not int
            or event["step"] < 1
            or not isinstance(event.get("status"), str)
            or not event["status"].strip()
            for event in trace
        ):
            raise ValueError("experience trace evidence invalid")
        steps.extend(
            event["step"] for event in trace if event["status"] == "experience_unavailable"
        )
    return steps


def compare_experience_runs(baseline: dict, candidate: dict) -> dict:
    """Compare complete reports differing only in the declared experience treatment.

    Hashes identify caller-supplied evidence, not authenticity or independence.
    Statistical inference and repeated/unseen qualification are separate work.
    """
    summaries = [validate_report(report) for report in (baseline, candidate)]
    normalized = []
    for index, report in enumerate((baseline, candidate)):
        if report.get("run_status") != "completed":
            raise ValueError("experience comparison requires completed runs")
        requested, population = report.get("requested_n"), report.get("population_n")
        if (
            type(requested) is not int
            or requested < 1
            or type(population) is not int
            or population < 1
            or len(report["results"]) != min(requested, population)
        ):
            raise ValueError("experience comparison requires the full planned denominator")
        protocol = report.get("protocol")
        if not isinstance(protocol, dict) or protocol.get("experience_reuse") is not bool(index):
            raise ValueError("experience comparison requires off/on arms")
        digest = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
        if report["protocol_hash"] != digest:
            raise ValueError("experience protocol hash mismatch")
        if index:
            if not re.fullmatch(
                r"[a-f0-9]{64}", protocol.get("experience_store_hash", "")
            ) or not re.fullmatch(
                r"sha256:[a-f0-9]{64}", protocol.get("experience_scope_hash", "")
            ):
                raise ValueError("experience treatment provenance missing")
        elif _TREATMENT.intersection(protocol) != {"experience_reuse"}:
            raise ValueError("off arm contains experience provenance")
        system = deepcopy(report.get("system_fingerprint"))
        if not isinstance(system, dict) or not system:
            raise ValueError("experience system fingerprint missing")
        experience = system.pop("experience", None)
        expected = {
            "enabled": bool(index),
            "scope_hash": protocol.get("experience_scope_hash"),
            "source_hash": experience.get("source_hash")
            if isinstance(experience, dict) and index
            else None,
        }
        if experience != expected or (
            index and not re.fullmatch(r"[a-f0-9]{64}", experience.get("source_hash") or "")
        ):
            raise ValueError("experience system treatment mismatch")
        normalized.append(
            ({key: value for key, value in protocol.items() if key not in _TREATMENT}, system)
        )
    if normalized[0] != normalized[1]:
        raise ValueError("incomparable non-experience protocol or system")
    for key in (
        "benchmark",
        "cohort_hash",
        "source_manifest_hash",
        "runner_hash",
        "model",
        "model_route",
        "python",
        "tool_profile",
    ):
        if not baseline.get(key) or baseline[key] != candidate.get(key):
            raise ValueError("incomparable experience " + key)
    for key in ("requested_n", "population_n"):
        if baseline[key] != candidate[key]:
            raise ValueError("incomparable experience " + key)
    old, new = baseline["results"], candidate["results"]
    ids = [row["case_id"] for row in old]
    if not ids or len(set(ids)) != len(ids) or ids != [row["case_id"] for row in new]:
        raise ValueError("incomplete or reordered experience cohort")
    for before, after in zip(old, new, strict=True):
        if not before.get("database_hash") or before["database_hash"] != after.get("database_hash"):
            raise ValueError("experience database snapshot changed")
        if any(before[key] != after[key] for key in ("sub_total", "difficulty")):
            raise ValueError("experience task scoring metadata changed")
    blockers = []
    service_counts = [
        sum(row["failure"] in SERVICE_FAILURES for row in rows) for rows in (old, new)
    ]
    if any(service_counts):
        blockers.append("service_failures_confound_attribution")
    if any(row.get("attempted") is not True for row in old + new):
        blockers.append("unattempted_or_unknown_tasks")
    if any(row.get("severe_incident") for row in old + new):
        blockers.append("severe_incident")
    if any(not summary["token_telemetry_complete"] for summary in summaries):
        blockers.append("incomplete_token_telemetry")
    unavailable = [_unavailable_steps(rows) for rows in (old, new)]
    if unavailable[0]:
        # The off arm must not process experience at all; an outage there is
        # evidence of the treatment, not a confound to weigh.
        raise ValueError("off arm recorded experience processing")
    if unavailable[1]:
        # Delivery elsewhere in the run cannot qualify steps where the
        # advisory port failed. Keep outcomes, but flag the interrupted treatment.
        blockers.append("experience_unavailable_confound_attribution")
    delivery = [_delivered_hints(rows) for rows in (old, new)]
    if delivery[0]["cases_with_hints"]:
        raise ValueError("off arm received experience")
    if delivery[0]["dropped"]:
        raise ValueError("off arm contains experience budget drops")
    if not delivery[1]["cases_with_hints"]:
        # The declared treatment never reached a task: observed outcomes remain
        # reported but cannot be attributed to experience reuse.
        blockers.append("experience_treatment_not_delivered")
    improvements = [
        a["case_id"] for a, b in zip(old, new, strict=True) if not a["passed"] and b["passed"]
    ]
    regressions = [
        a["case_id"] for a, b in zip(old, new, strict=True) if a["passed"] and not b["passed"]
    ]
    return {
        "comparison": "experience-off-on-development-v1",
        "benchmark": baseline["benchmark"],
        "n": len(ids),
        "report_hashes": [canonical_report_hash(report) for report in (baseline, candidate)],
        "protocol_hashes": [baseline["protocol_hash"], candidate["protocol_hash"]],
        "treatment": {key: candidate["protocol"][key] for key in sorted(_TREATMENT)},
        "baseline_passed": summaries[0]["passed"],
        "candidate_passed": summaries[1]["passed"],
        "improvements": improvements,
        "regressions": regressions,
        "pass_rate_delta": (len(improvements) - len(regressions)) / len(ids),
        "service_failures": dict(zip(("baseline", "candidate"), service_counts, strict=True)),
        "efficiency": {
            name: {
                key: summary[key]
                for key in (
                    "tokens",
                    "token_telemetry_complete",
                    "p50_seconds",
                    "p95_seconds",
                    "cost_usd",
                )
            }
            for name, summary in zip(("baseline", "candidate"), summaries, strict=True)
        },
        "failure_transitions": dict(
            Counter(
                (a["failure"] or "passed") + "->" + (b["failure"] or "passed")
                for a, b in zip(old, new, strict=True)
            )
        ),
        "experience_delivery": {
            "baseline_cases_with_hints": delivery[0]["cases_with_hints"],
            "candidate_cases_with_hints": delivery[1]["cases_with_hints"],
            "candidate_hints": delivery[1]["hints"],
            "candidate_hints_dropped": delivery[1]["dropped"],
            "by_failure_code": delivery[1]["by_failure_code"],
        },
        "blocked_by": blockers,
        "statistical_inference": "not_run",
        "independent_review": "not_run",
        "unseen_evaluation": "not_run",
        "repeated_pair_qualification": "not_run",
        "production_accepted": False,
    }
