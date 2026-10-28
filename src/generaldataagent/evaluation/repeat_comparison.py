"""Paired comparison over repeated runs of one frozen cohort.

Stage 10's exit criteria ask for at least three complete paired repeats, per-task
gains and regressions, a confidence interval, the accuracy-token-latency relation
and real cost. ``compare_runs`` answers none of those: it pairs one run with one
run, and pooling task-runs as independent observations would overstate precision
because repeats of the same task are correlated.

Every task therefore contributes one pair of repeat vectors. The interval comes
from resampling tasks (not runs), and the exact test counts tasks whose pass count
moved. Nothing is excluded: an unattempted or failed run is a failed observation.
The cohort status is a caller declaration; nothing here can verify that a cohort
was never used during development.
"""

import json
import math
import random
from collections import Counter

from generaldataagent.evaluation.stage_six_comparison import SERVICE_FAILURES
from generaldataagent.evaluation.stage_six_gates import validate_report
from generaldataagent.evaluation.stage_six_stability import repeated_evidence

REPEATS_REQUIRED = 3
COHORT_STATUSES = {"development", "held_out"}
SAME_ACROSS_ARMS = (
    "benchmark",
    "cohort_hash",
    "source_manifest_hash",
    "model",
    "model_route",
    "requested_n",
    "population_n",
)


def _stability(runs: list[dict]) -> dict | None:
    """Within-arm repeat evidence; one run cannot establish its own comparability."""
    return repeated_evidence(runs) if len(runs) >= 2 else None


def _seconds_quantile(times: list[float], q: float) -> float:
    ordered = sorted(times)
    return ordered[math.ceil(q * len(ordered)) - 1]


def _arm(name: str, runs: list[dict]) -> dict:
    rows = [row for run in runs for row in run["results"]]
    passes = sum(bool(row["passed"]) for row in rows)
    tokens = sum(row["tokens"] for row in rows)
    times = [row["elapsed_seconds"] for row in rows]
    measured = all(row.get("cost_usd") is not None for row in rows)
    cost = sum(row["cost_usd"] for row in rows) if measured else None
    return {
        "name": name,
        "runs": len(runs),
        "n": len(runs[0]["results"]),
        "case_runs": len(rows),
        "passes": passes,
        "pooled_pass_rate": passes / len(rows),
        "per_run_pass_rates": [run["summary"]["pass_at_1"] for run in runs],
        "tokens": tokens,
        "mean_tokens_per_case": tokens / len(rows),
        "tokens_per_pass": tokens / passes if passes else None,
        "p50_seconds": _seconds_quantile(times, 0.5),
        "p95_seconds": _seconds_quantile(times, 0.95),
        "cost_usd": cost,
        "cost_per_pass": cost / passes if cost is not None and passes else None,
        "cost_verified": measured,
        "failures": dict(Counter(row["failure"] for row in rows if not row["passed"])),
        "service_errors": sum(row["failure"] in SERVICE_FAILURES for row in rows),
        "protocol_hashes": sorted({run["protocol_hash"] for run in runs}),
        "system_fingerprints": sorted(
            json.dumps(run["system_fingerprint"], sort_keys=True) for run in runs
        ),
    }


def _per_case(ids, baseline, candidate) -> list[dict]:
    rows = []
    for index, case_id in enumerate(ids):
        base = [run["results"][index] for run in baseline]
        cand = [run["results"][index] for run in candidate]
        if base[0]["case_id"] != case_id or cand[0]["case_id"] != case_id:
            raise ValueError("arms evaluated different cohorts")
        base_passes = sum(bool(row["passed"]) for row in base)
        cand_passes = sum(bool(row["passed"]) for row in cand)
        rows.append(
            {
                "case_id": case_id,
                "difficulty": base[0]["difficulty"],
                "baseline_passes": base_passes,
                "baseline_repeats": len(base),
                "baseline_rate": base_passes / len(base),
                "candidate_passes": cand_passes,
                "candidate_repeats": len(cand),
                "candidate_rate": cand_passes / len(cand),
                "rate_delta": cand_passes / len(cand) - base_passes / len(base),
                "outcome": (
                    "gain"
                    if cand_passes > base_passes
                    else "regression"
                    if cand_passes < base_passes
                    else "stable"
                ),
                "baseline_mean_tokens": sum(row["tokens"] for row in base) / len(base),
                "candidate_mean_tokens": sum(row["tokens"] for row in cand) / len(cand),
                "baseline_mean_seconds": sum(row["elapsed_seconds"] for row in base) / len(base),
                "candidate_mean_seconds": sum(row["elapsed_seconds"] for row in cand) / len(cand),
            }
        )
    return rows


def _quantile(ordered: list[float], q: float) -> float:
    position = q * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap_interval(rows: list[dict], samples: int, seed: int) -> list[float]:
    """Resample tasks, not task-runs: repeats within a task are correlated."""
    rng = random.Random(seed)
    values = []
    for _ in range(samples):
        base_passes = base_runs = cand_passes = cand_runs = 0
        for _ in range(len(rows)):
            case = rows[rng.randrange(len(rows))]
            base_passes += case["baseline_passes"]
            base_runs += case["baseline_repeats"]
            cand_passes += case["candidate_passes"]
            cand_runs += case["candidate_repeats"]
        values.append(cand_passes / cand_runs - base_passes / base_runs)
    values.sort()
    return [_quantile(values, 0.025), _quantile(values, 0.975)]


def _sign_test(gains: int, regressions: int) -> float:
    discordant = gains + regressions
    if not discordant:
        return 1.0
    tail = sum(math.comb(discordant, i) for i in range(min(gains, regressions) + 1))
    return min(1.0, 2 * tail / 2**discordant)


def compare_repeated_runs(
    baseline: list[dict],
    candidate: list[dict],
    *,
    bootstrap_samples: int = 2000,
    bootstrap_seed: int = 20261020,
    cohort_status: str = "development",
    max_service_error_rate: float = 0.05,
) -> dict:
    """Pair two arms of repeated runs; report, never certify general improvement."""
    if cohort_status not in COHORT_STATUSES:
        raise ValueError("invalid cohort status")
    if not baseline or not candidate:
        raise ValueError("both arms need at least one run")
    if bootstrap_samples < 100:
        raise ValueError("too few bootstrap samples for an interval")
    if not 0 <= max_service_error_rate <= 1:
        raise ValueError("invalid service-error policy")
    for runs in (baseline, candidate):
        for report in runs:
            validate_report(report)
    left, right = baseline[0], candidate[0]
    for key in SAME_ACROSS_ARMS:
        if left.get(key) != right.get(key):
            raise ValueError("arms differ in " + key)
    ids = [row["case_id"] for row in left["results"]]
    if [row["case_id"] for row in right["results"]] != ids:
        raise ValueError("arms evaluated different cohorts")
    databases = [row["database_hash"] for row in left["results"]]
    if [row["database_hash"] for row in right["results"]] != databases:
        raise ValueError("arms evaluated different database snapshots")

    evidence = {"baseline": _stability(baseline), "candidate": _stability(candidate)}
    arms = {"baseline": _arm("baseline", baseline), "candidate": _arm("candidate", candidate)}
    rows = _per_case(ids, baseline, candidate)
    paired = [row for row in rows if row["outcome"] != "stable"]
    gains = sum(row["outcome"] == "gain" for row in rows)
    regressions = sum(row["outcome"] == "regression" for row in rows)
    delta = arms["candidate"]["pooled_pass_rate"] - arms["baseline"]["pooled_pass_rate"]
    interval = _bootstrap_interval(rows, bootstrap_samples, bootstrap_seed)
    service_rates = {name: arms[name]["service_errors"] / arms[name]["case_runs"] for name in arms}

    blockers = []
    for name in ("baseline", "candidate"):
        if evidence[name] is None:
            blockers.append(f"{name}:single_run_arm")
        else:
            blockers.extend(f"{name}:{reason}" for reason in evidence[name]["blocked_by"])
    if cohort_status != "held_out":
        blockers.append("cohort_not_held_out")
    if max(service_rates.values()) > max_service_error_rate:
        blockers.append("service_unreliable")
    if not all(arms[name]["cost_verified"] for name in arms):
        blockers.append("cost_not_verified")
    if regressions:
        blockers.append("case_regressions")
    if delta <= 0:
        blockers.append("no_measured_gain")
    if interval[0] <= 0:
        blockers.append("confidence_interval_includes_zero")

    return {
        "benchmark": left["benchmark"],
        "cohort_hash": left["cohort_hash"],
        "cohort_status": cohort_status,
        "n": len(ids),
        "case_ids": ids,
        "repeats_required": REPEATS_REQUIRED,
        "arms": arms,
        "system_changed_between_arms": arms["baseline"]["system_fingerprints"]
        != arms["candidate"]["system_fingerprints"],
        "protocol_hash_changed": arms["baseline"]["protocol_hashes"]
        != arms["candidate"]["protocol_hashes"],
        "per_case": rows,
        # Within-arm reliability, so a flip rate that blocks the claim is visible
        # rather than only named in a blocker string.
        "stability": evidence,
        "comparison": {
            "pass_rate_delta": delta,
            "bootstrap_95": interval,
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_seed": bootstrap_seed,
            "bootstrap_unit": "task",
            "gains": gains,
            "regressions": regressions,
            "discordant_tasks": len(paired),
            "sign_test_exact_two_sided_p": _sign_test(gains, regressions),
        },
        "service_error_rates": service_rates,
        "design_blockers": blockers,
        "criteria_met": not blockers,
        "independent_review": "not_supplied",
        "production_accepted": False,
        "notes": [
            "cohort_status is a caller declaration; this tool cannot verify that "
            "the cohort was never used during development",
            "the interval resamples tasks, so repeats of one task are not treated as "
            "independent observations",
            "a failed or unattempted run counts as a failed observation; nothing is excluded",
            "independent review and production shadow validation are external gates and "
            "are never supplied by this comparison",
        ],
    }
