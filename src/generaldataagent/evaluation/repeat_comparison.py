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

from generaldataagent.evaluation.frozen_cohort import verify_cohort_file
from generaldataagent.evaluation.run_report import RunReportInput, ValidatedRunReport
from generaldataagent.evaluation.stage_six_comparison import SERVICE_FAILURES
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


def _stability(runs: list[RunReportInput]) -> dict | None:
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


def tasks_for_power(
    discordance_rate: float,
    *,
    target_power: float = 0.8,
    alpha: float = 0.05,
    direction_share: float = 0.8,
    max_discordant: int = 200,
) -> dict | None:
    """Estimate the task count a one-directional gain would need to be detected.

    The comparison's exact test is two-sided with p = 2 * 0.5**m when all m
    discordant tasks move the same way, so it cannot reject below six discordant
    tasks at alpha 0.05. Dividing that count by the observed discordance rate
    gives the task count, under the assumption that both the rate and the share
    of discordant tasks that move the same way stay as observed. It is a planning
    aid, not evidence, and it is None when no rate was observed.
    """
    if not 0 <= discordance_rate <= 1:
        raise ValueError("invalid discordance rate")
    if not 0.5 < direction_share <= 1:
        raise ValueError("direction share must exceed chance")
    if not 0 < target_power < 1 or not 0 < alpha < 1:
        raise ValueError("invalid power policy")
    if not discordance_rate:
        return None
    critical = next(m for m in range(1, 1000) if 2 * 0.5**m <= alpha)
    for discordant in range(critical, max_discordant + 1):
        probability = sum(
            math.comb(discordant, k)
            * direction_share**k
            * (1 - direction_share) ** (discordant - k)
            for k in range(critical, discordant + 1)
        )
        if probability >= target_power:
            return {
                "discordant_tasks_needed": discordant,
                "tasks_needed": math.ceil(discordant / discordance_rate),
                "critical_discordant_tasks": critical,
                "observed_discordance_rate": discordance_rate,
                "direction_share": direction_share,
                "target_power": target_power,
                "alpha": alpha,
            }
    return None


def _binomial_pmf(repeats: int, probability: float) -> list[float]:
    return [
        math.comb(repeats, k) * probability**k * (1 - probability) ** (repeats - k)
        for k in range(repeats + 1)
    ]


def _move_probabilities(baseline_rate: float, candidate_rate: float, repeats: int):
    """Probability that one task's pass count rises and that it falls.

    Both arms draw independently from their own binomial, so a task that passes
    every repeat in one arm and every repeat in the other cannot move.
    """
    base = _binomial_pmf(repeats, baseline_rate)
    cand = _binomial_pmf(repeats, candidate_rate)
    gain = sum(base[b] * sum(cand[b + 1 :]) for b in range(repeats))
    loss = sum(base[b] * sum(cand[:b]) for b in range(repeats))
    return max(0.0, gain), max(0.0, loss)


def minimum_detectable_effect(
    rows: list[dict],
    *,
    target_power: float = 0.8,
    alpha: float = 0.05,
    step: float = 0.02,
) -> dict | None:
    """Smallest per-task pass-rate shift this cohort could detect at target power.

    The comparison's inferential claim rests on the exact two-sided sign test
    over discordant tasks, so the same test defines what "detect" means here.
    For each candidate shift the observed per-task baseline rates are raised by
    that amount (capped at 1), each task then contributes +1, 0 or -1 with
    probabilities from two independent binomials over the recorded repeat count,
    and the distribution of gain/regression counts is convolved exactly over
    tasks. The returned shift is the first on the grid whose rejection
    probability reaches ``target_power``.

    It is a property of this cohort, this repeat count and these observed rates
    — not of the treatment and not evidence of an effect. Tasks already at
    ceiling cannot move and therefore only make the estimate more conservative.
    ``None`` means no shift below 1.0 reached the target power.
    """
    if not 0 < target_power < 1 or not 0 < alpha < 1:
        raise ValueError("invalid power policy")
    if not 0 < step <= 0.5:
        raise ValueError("invalid effect grid step")
    rates = []
    for row in rows:
        repeats = row["baseline_repeats"]
        if row["candidate_repeats"] != repeats:
            raise ValueError("the effect estimate needs equal repeats per arm")
        if repeats < 1:
            raise ValueError("the effect estimate needs at least one repeat")
        rates.append((row["baseline_passes"] / repeats, repeats))
    if not rates:
        raise ValueError("the effect estimate needs at least one task")
    tasks = len(rates)
    rejects = [
        [_sign_test(gains, regressions) <= alpha for regressions in range(tasks + 1)]
        for gains in range(tasks + 1)
    ]
    for index in range(1, int(round(1 / step)) + 1):
        shift = round(index * step, 10)
        distribution = {(0, 0): 1.0}
        for baseline_rate, repeats in rates:
            candidate_rate = min(1.0, baseline_rate + shift)
            gain, loss = _move_probabilities(baseline_rate, candidate_rate, repeats)
            moved = {}
            for (gains, regressions), probability in distribution.items():
                for dg, dr, weight in (
                    (1, 0, gain),
                    (0, 1, loss),
                    (0, 0, 1.0 - gain - loss),
                ):
                    if weight <= 1e-12:
                        continue
                    key = (gains + dg, regressions + dr)
                    moved[key] = moved.get(key, 0.0) + probability * weight
            distribution = moved
        power = sum(p for (g, r), p in distribution.items() if rejects[g][r])
        if power >= target_power:
            return {
                "per_task_rate_shift": shift,
                "expected_pooled_rate_delta": sum(
                    min(1.0, rate + shift) - rate for rate, _ in rates
                )
                / tasks,
                "power": power,
                "target_power": target_power,
                "alpha": alpha,
                "n": tasks,
                "repeats_per_arm": rates[0][1],
                "decision_rule": "exact two-sided sign test over discordant tasks",
            }
    return None


def _cost_report(baseline: list[dict], candidate: list[dict], arms: dict) -> dict:
    """Say what cost reporting lost and what it would take to get it back.

    A null cost is not a cheap run: it is a missing measurement. The gate that
    depends on it stays closed, and this comparison can never open it.
    """
    unmeasured = {}
    unmeasured_tasks = {}
    for name, runs in (("baseline", baseline), ("candidate", candidate)):
        runs_without_cost = [
            run.get("started_at", f"run-{index}")
            for index, run in enumerate(runs)
            if any(row.get("cost_usd") is None for row in run["results"])
        ]
        if runs_without_cost:
            unmeasured[name] = runs_without_cost
            unmeasured_tasks[name] = sum(
                1 for run in runs for row in run["results"] if row.get("cost_usd") is None
            )
    measured = not unmeasured
    if measured:
        degradation = None
    else:
        degradation = (
            "cost is unknown for "
            + ", ".join(sorted(unmeasured))
            + ": cost per pass is null rather than zero, and tokens are reported as a "
            "proxy only because they are not a price"
        )
    return {
        "status": "measured" if measured else "unavailable",
        "measured_arms": sorted(name for name in arms if name not in unmeasured),
        "unmeasured_arms": sorted(unmeasured),
        "unmeasured_runs": unmeasured,
        "unmeasured_task_runs": unmeasured_tasks,
        "degradation": degradation,
        "criteria_impact": (
            "cost conditions are met by measurement only"
            if measured
            else "cost_not_verified stays in design_blockers and this tool cannot clear it; "
            "clearing it needs metered routing or an explicit external ruling that records "
            "the cost gate as unavailable"
        ),
    }


def compare_repeated_runs(
    baseline: list[RunReportInput],
    candidate: list[RunReportInput],
    *,
    bootstrap_samples: int = 2000,
    bootstrap_seed: int = 20261020,
    cohort_status: str = "development",
    max_service_error_rate: float = 0.05,
    cohort_file: dict | None = None,
    cohort_file_sha256: str | None = None,
) -> dict:
    """Pair two arms of repeated runs; report, never certify general improvement."""
    if cohort_status not in COHORT_STATUSES:
        raise ValueError("invalid cohort status")
    if (cohort_file is None) != (cohort_file_sha256 is None):
        raise ValueError("verifying a frozen file needs both the file and its hash")
    if not baseline or not candidate:
        raise ValueError("both arms need at least one run")
    if bootstrap_samples < 100:
        raise ValueError("too few bootstrap samples for an interval")
    if not 0 <= max_service_error_rate <= 1:
        raise ValueError("invalid service-error policy")
    baseline_runs = [ValidatedRunReport.from_report(report) for report in baseline]
    candidate_runs = [ValidatedRunReport.from_report(report) for report in candidate]
    baseline = [run.to_dict() for run in baseline_runs]
    candidate = [run.to_dict() for run in candidate_runs]
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
    # Self-reported agreement is all a pair of reports can show; reading the file
    # itself is what ties them to the declaration a reviewer holds.
    verification = (
        verify_cohort_file(baseline + candidate, cohort_file, file_sha256=cohort_file_sha256)
        if cohort_file is not None
        else None
    )

    evidence = {"baseline": _stability(baseline_runs), "candidate": _stability(candidate_runs)}
    arms = {"baseline": _arm("baseline", baseline), "candidate": _arm("candidate", candidate)}
    cohort_files = {
        name: {run.get("cohort_file_hash") for run in runs}
        for name, runs in (("baseline", baseline), ("candidate", candidate))
    }
    if cohort_status == "held_out":
        # A held-out claim is only checkable if the run names the file it was
        # declared from; otherwise the declaration is unfalsifiable.
        if any(None in hashes for hashes in cohort_files.values()):
            raise ValueError("a held-out claim requires a frozen cohort file on every run")
        if cohort_files["baseline"] != cohort_files["candidate"]:
            raise ValueError("arms used different frozen cohort files")
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
        "cohort_file_hashes": {
            name: sorted(h for h in hashes if h) for name, hashes in cohort_files.items()
        },
        "cohort_file_verification": verification,
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
        "cost": _cost_report(baseline, candidate, arms),
        "design": {
            "discordance_rate": len(paired) / len(rows),
            "tasks_for_target_power": tasks_for_power(len(paired) / len(rows)),
            "minimum_detectable_effect": minimum_detectable_effect(rows),
        },
        "design_blockers": blockers,
        "criteria_met": not blockers,
        "independent_review": "not_supplied",
        "production_accepted": False,
        "notes": [
            "cohort_status is a caller declaration; this tool cannot verify that "
            "the cohort was never used during development",
            "cohort_file_verification is null until the frozen file is supplied; without "
            "it the two arms agree on a self-reported hash, which is not evidence that "
            "either ran the file a reviewer holds",
            "the interval resamples tasks, so repeats of one task are not treated as "
            "independent observations",
            "a failed or unattempted run counts as a failed observation; nothing is excluded",
            "tasks_for_target_power divides a discordant-task requirement by the observed "
            "discordance rate; it is a planning estimate, not evidence",
            "minimum_detectable_effect simulates a shift applied to the observed per-task "
            "baseline rates and asks how often the exact sign test would reject; it "
            "describes this cohort, not the treatment, and is null when no shift below "
            "1.0 reaches the target power",
            "cost is summed only when every task-run of an arm carries a measured "
            "cost_usd; a null cost is reported as unknown, never as zero",
            "independent review and production shadow validation are external gates and "
            "are never supplied by this comparison",
        ],
    }
