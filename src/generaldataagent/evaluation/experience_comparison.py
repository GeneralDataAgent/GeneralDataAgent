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
        "blocked_by": blockers,
        "statistical_inference": "not_run",
        "independent_review": "not_run",
        "unseen_evaluation": "not_run",
        "repeated_pair_qualification": "not_run",
        "production_accepted": False,
    }
