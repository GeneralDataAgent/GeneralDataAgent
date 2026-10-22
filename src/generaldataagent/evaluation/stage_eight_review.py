"""Offline Stage 8 comparison; historical records are protection, not new scores."""

from generaldataagent.evaluation.stage_six_comparison import compare_runs


def review_stage8(baseline, candidate, historical=None):
    for report in (baseline, candidate):
        if report.get("protocol", {}).get("version") not in {"stage8-v1", "stage8-v2"}:
            raise ValueError("fresh matching Stage 8 paired runs required")
        if report.get("run_status") != "completed":
            raise ValueError("completed paired runs required")
    if baseline.get("model_route") != candidate.get("model_route"):
        raise ValueError("paired model route differs")
    comparison = compare_runs(baseline, candidate)
    before = {r["case_id"]: r for r in baseline["results"]}
    after = {r["case_id"]: r for r in candidate["results"]}
    for key in before:
        if not before[key].get("database_hash") or before[key]["database_hash"] != after[key].get(
            "database_hash"
        ):
            raise ValueError("paired database mismatch")
    protected = {key for key in before if before[key]["passed"]}
    historical_keys = {r["case_id"] for r in (historical or {}).get("results", []) if r["passed"]}
    if historical and historical.get("benchmark") != baseline["benchmark"]:
        raise ValueError("historical benchmark mismatch")
    historical_missing = sorted(historical_keys - set(after))
    historical_regressed = sorted(
        key for key in historical_keys & set(after) if not after[key]["passed"]
    )
    return {
        "strict_comparison": comparison,
        "fresh_correct_protection": {
            "n": len(protected),
            "regressed": sorted(key for key in protected if not after[key]["passed"]),
        },
        "historical_correct_protection": {
            "n": len(historical_keys),
            "missing": historical_missing,
            "regressed": historical_regressed,
        },
        "strict_score_changed": False,
        "denominator_changed": False,
        "production_accepted": False,
        "official_leaderboard_comparable": False,
        "limits": (
            "one public-development pair; repeated trials, unseen tasks "
            "and independent review remain required"
        ),
    }
