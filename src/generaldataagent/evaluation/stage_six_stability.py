"""Repeated-run reliability evidence bound to identical cohorts, data and system settings."""

import hashlib
import json
import statistics

from generaldataagent.evaluation.stage_six_gates import REQUIRED_TRACKS, validate_report


def canonical_report_hash(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, allow_nan=False).encode()).hexdigest()


def repeated_evidence(
    reports: list[dict], *, max_flip_rate: float = 0.1, max_service_error_rate: float = 0.05
) -> dict:
    if not 0 <= max_flip_rate <= 1 or not 0 <= max_service_error_rate <= 1:
        raise ValueError("invalid repeat policy")
    if len(reports) < 2:
        raise ValueError("at least two independently recorded runs required")
    hashes = [canonical_report_hash(r) for r in reports]
    if len(set(hashes)) != len(hashes):
        raise ValueError("duplicate report is not an independent repeated run")
    first = reports[0]
    for report in reports:
        validate_report(report)
        for key in [
            "benchmark",
            "cohort_hash",
            "protocol_hash",
            "source_manifest_hash",
            "runner_hash",
            "model",
            "system_fingerprint",
            "model_route",
        ]:
            if key not in report or report[key] != first[key]:
                raise ValueError("incomparable repeated-run " + key)
        if [r["case_id"] for r in report["results"]] != [r["case_id"] for r in first["results"]]:
            raise ValueError("repeated cohort is incomplete or reordered")
        if [r["database_hash"] for r in report["results"]] != [
            r["database_hash"] for r in first["results"]
        ]:
            raise ValueError("repeated database snapshot changed")
    matrix = [[r["passed"] for r in report["results"]] for report in reports]
    rates = [report["summary"]["pass_at_1"] for report in reports]
    n = len(matrix[0])
    repeats = len(matrix)
    flipped = [
        first["results"][i]["case_id"] for i in range(n) if len({run[i] for run in matrix}) > 1
    ]
    always = sum(all(run[i] for run in matrix) for i in range(n))
    ever = sum(any(run[i] for run in matrix) for i in range(n))
    services = [
        sum(
            r["failure"]
            in {"model_error", "worker_error", "worker_deadline", "service_circuit_open"}
            for r in report["results"]
        )
        / n
        for report in reports
    ]
    blockers = []
    if any(report.get("run_status") != "completed" for report in reports):
        blockers.append("incomplete_run_status")
    if any(
        type(report.get("requested_n")) is not int
        or report["requested_n"] < 1
        or type(report.get("population_n")) is not int
        or report["population_n"] < 1
        or len(report["results"]) != min(report["requested_n"], report["population_n"])
        for report in reports
    ):
        blockers.append("incomplete_planned_cohort")
    if repeats < 3:
        blockers.append("fewer_than_three_runs")
    if n < REQUIRED_TRACKS.get(first["benchmark"], 10**9):
        blockers.append("insufficient_cohort")
    if len(flipped) / n > max_flip_rate:
        blockers.append("task_outcomes_unstable")
    if max(services) > max_service_error_rate:
        blockers.append("service_unreliable")
    if any(not row.get("attempted", True) for r in reports for row in r["results"]):
        blockers.append("unattempted_tasks")
    if any(row.get("severe_incident") for r in reports for row in r["results"]):
        blockers.append("severe_incident")
    return {
        "benchmark": first["benchmark"],
        "cohort_hash": first["cohort_hash"],
        "protocol_hash": first["protocol_hash"],
        "system_fingerprint": first["system_fingerprint"],
        "report_hashes": hashes,
        "n": n,
        "repeats": repeats,
        "mean_pass_rate": statistics.mean(rates),
        "worst_pass_rate": min(rates),
        "pass_rate_population_variance": statistics.pvariance(rates),
        "all_runs_pass_rate": always / n,
        "at_least_one_run_pass_rate": ever / n,
        "flipped_case_ids": flipped,
        "flip_rate": len(flipped) / n,
        "service_error_rates": services,
        "qualified": not blockers,
        "blocked_by": blockers,
        "production_accepted": False,
    }
