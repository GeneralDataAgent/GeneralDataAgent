"""Descriptive Skill off/on diagnostics without weakening the general pairing gate."""

import hashlib
import json
import re
from collections import Counter

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.skill_bundle import ordered_skill_definitions
from generaldataagent.evaluation.run_report import RunReportInput, ValidatedRunReport
from generaldataagent.evaluation.skill_treatment import TREATMENT_SOURCE_PATHS, SkillTreatment
from generaldataagent.evaluation.stage_six_comparison import SERVICE_FAILURES

_REFUSALS = {"skill_admission_refused", "skill_action_not_allowed"}
_INSTRUCTION_FIELDS = {
    "mode",
    "references",
    "allowed_actions",
    "rendered_hash",
    "model_responses",
    "budget_scope",
}


def _failure_label(row):
    return row["failure"] or ("passed" if row["passed"] else "unrecorded_failure")


def _trace(row):
    trace = row.get("trace")
    if not isinstance(trace, list) or any(
        not isinstance(event, dict)
        or not isinstance(event.get("status"), str)
        or not event["status"].strip()
        for event in trace
    ):
        raise ValueError("Skill trace evidence invalid")
    return trace


def _delivered(row, summary, references, actions):
    metadata = row.get("skill_treatment")
    if metadata == {**summary, "delivery_status": "unknown_worker_failure"}:
        if row["failure"] not in {"worker_error", "worker_deadline"} or "skill_extension" in row:
            raise ValueError("Skill unknown-worker evidence conflicts with result")
        return {
            "case_id": row["case_id"],
            "status": "unknown_worker_failure",
            "model_responses": None,
        }
    if metadata == {**summary, "delivery_status": "not_attempted"}:
        if row.get("attempted") is not False or "skill_extension" in row:
            raise ValueError("Skill unattempted evidence conflicts with result")
        return {"case_id": row["case_id"], "status": "not_attempted", "model_responses": None}
    if metadata != summary:
        raise ValueError("Skill case treatment mismatch")
    instruction = row.get("skill_extension")
    if (
        not isinstance(instruction, dict)
        or set(instruction) != _INSTRUCTION_FIELDS
        or instruction["mode"] != "admitted_instructions"
        or instruction["references"] != references
        or instruction["allowed_actions"] != actions
        or instruction["budget_scope"] != "declaration_compatibility; existing task meters"
        or not isinstance(instruction["rendered_hash"], str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", instruction["rendered_hash"])
        or type(instruction["model_responses"]) is not int
        or instruction["model_responses"] < 0
        or row.get("attempted") is False
    ):
        raise ValueError("Skill instruction delivery evidence invalid")
    return {
        "case_id": row["case_id"],
        "status": "confirmed_response" if instruction["model_responses"] else "unconfirmed",
        "model_responses": instruction["model_responses"],
    }


def compare_skill_runs(
    baseline: RunReportInput, candidate: RunReportInput, treatment_snapshot: dict
) -> dict:
    """Allow only the declared Skill treatment difference; never read a live registry.

    Exact hashes and delivery counters are caller/worker evidence, not signed
    authentication, proof of model compliance or an independent effect estimate.
    """
    runs = [ValidatedRunReport.from_report(report) for report in (baseline, candidate)]
    baseline, candidate = (run.to_dict() for run in runs)
    treatment = SkillTreatment.from_payload(treatment_snapshot)
    summary = treatment.summary()
    body = treatment.payload()["body"]
    root = SkillDefinition.model_validate(body["skill"])
    ordered = ordered_skill_definitions(
        root,
        {key: SkillDefinition.model_validate(value) for key, value in body["dependencies"].items()},
    )
    references = [
        ["skill", value.name, value.version, ExtensionAdmission.skill_hash(value)]
        for value in ordered
    ]
    normalized = []
    for index, report in enumerate((baseline, candidate)):
        if report.get("run_status") != "completed":
            raise ValueError("Skill comparison requires completed runs")
        requested, population = report.get("requested_n"), report.get("population_n")
        if (
            type(requested) is not int
            or requested < 1
            or type(population) is not int
            or population < 1
            or len(report["results"]) != min(requested, population)
        ):
            raise ValueError("Skill comparison requires the full planned denominator")
        protocol = report.get("protocol")
        system = report.get("system_fingerprint")
        if not isinstance(protocol, dict) or not isinstance(system, dict) or not system:
            raise ValueError("Skill comparison protocol or system missing")
        digest = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
        if digest != report["protocol_hash"]:
            raise ValueError("Skill protocol hash mismatch")
        if index:
            if (
                protocol.get("skill_treatment") != summary
                or report.get("skill_treatment") != summary
            ):
                raise ValueError("Skill treatment snapshot mismatch")
            fingerprint = system.get("skill_treatment")
            if not isinstance(fingerprint, dict) or set(fingerprint) != set(summary) | {
                "source_hashes"
            }:
                raise ValueError("Skill system treatment mismatch")
            sources = fingerprint["source_hashes"]
            if (
                {key: value for key, value in fingerprint.items() if key != "source_hashes"}
                != summary
                or not isinstance(sources, dict)
                or set(sources) != set(TREATMENT_SOURCE_PATHS)
                or any(
                    not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                    for value in sources.values()
                )
            ):
                raise ValueError("Skill system treatment mismatch")
        elif any("skill_treatment" in value for value in (report, protocol, system)):
            raise ValueError("Skill comparison requires off/on arms")
        normalized.append(
            (
                {key: value for key, value in protocol.items() if key != "skill_treatment"},
                {key: value for key, value in system.items() if key != "skill_treatment"},
            )
        )
    if normalized[0] != normalized[1]:
        raise ValueError("incomparable non-Skill protocol or system")
    for key in (
        "benchmark",
        "cohort_hash",
        "source_manifest_hash",
        "runner_hash",
        "model",
        "model_route",
        "python",
        "tool_profile",
        "git_commit",
    ):
        if not baseline.get(key) or baseline[key] != candidate.get(key):
            raise ValueError("incomparable Skill " + key)
    for key in ("requested_n", "population_n", "cohort_file_hash"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError("incomparable Skill " + key)
    old, new = baseline["results"], candidate["results"]
    ids = [row["case_id"] for row in old]
    if not ids or len(set(ids)) != len(ids) or ids != [row["case_id"] for row in new]:
        raise ValueError("incomplete or reordered Skill cohort")
    deliveries = []
    for before, after in zip(old, new, strict=True):
        if not before.get("database_hash") or before["database_hash"] != after.get("database_hash"):
            raise ValueError("Skill database snapshot changed")
        if any(before.get(key) != after.get(key) for key in ("difficulty", "sub_total", "route")):
            raise ValueError("Skill task scoring or routing metadata changed")
        for row in (before, after):
            _trace(row)
        if (
            "skill_treatment" in before
            or "skill_extension" in before
            or any(event.get("code") in _REFUSALS for event in before["trace"])
            or before["failure"] in _REFUSALS
        ):
            raise ValueError("off arm contains Skill processing")
        delivery = _delivered(after, summary, references, sorted(root.allowed_tools))
        route = after.get("route")
        if route is not None and not isinstance(route, dict):
            raise ValueError("Skill routing evidence invalid")
        maximum = (route or {}).get("max_steps", candidate["protocol"].get("steps"))
        if type(maximum) is not int or maximum < 1:
            raise ValueError("Skill action budget evidence missing")
        if delivery["model_responses"] is not None and delivery["model_responses"] > maximum:
            raise ValueError("Skill response count exceeds task budget")
        if (
            delivery["status"] in {"unknown_worker_failure", "not_attempted", "unconfirmed"}
            and after["passed"]
        ):
            raise ValueError("Skill success lacks confirmed delivery")
        deliveries.append(delivery)
    summaries = [run.summary for run in runs]
    service_counts = [
        sum(row["failure"] in SERVICE_FAILURES for row in rows) for rows in (old, new)
    ]
    blockers = []
    if any(service_counts):
        blockers.append("service_failures_confound_attribution")
    if any(row.get("attempted") is not True for row in old + new):
        blockers.append("unattempted_or_unknown_tasks")
    if any(row.get("severe_incident") for row in old + new):
        blockers.append("severe_incident")
    if any(not value["token_telemetry_complete"] for value in summaries):
        blockers.append("incomplete_token_telemetry")
    if any(item["status"] != "confirmed_response" for item in deliveries):
        blockers.append("skill_delivery_unconfirmed")
    admission_refusals = [
        row["case_id"] for row in new if row["failure"] == "skill_admission_refused"
    ]
    if admission_refusals:
        blockers.append("skill_admission_unavailable_confound_attribution")
    improvements = [
        a["case_id"] for a, b in zip(old, new, strict=True) if not a["passed"] and b["passed"]
    ]
    regressions = [
        a["case_id"] for a, b in zip(old, new, strict=True) if a["passed"] and not b["passed"]
    ]
    return {
        "comparison": "skill-off-on-development-v1",
        "benchmark": baseline["benchmark"],
        "n": len(ids),
        "report_hashes": [run.report_hash for run in runs],
        "protocol_hashes": [baseline["protocol_hash"], candidate["protocol_hash"]],
        "treatment": summary,
        "treatment_effect_scope": "instructions_and_action_ceiling",
        "baseline_passed": summaries[0]["passed"],
        "candidate_passed": summaries[1]["passed"],
        "improvements": improvements,
        "regressions": regressions,
        "pass_rate_delta": (len(improvements) - len(regressions)) / len(ids),
        "service_failures": dict(zip(("baseline", "candidate"), service_counts, strict=True)),
        "efficiency": {
            name: {
                key: value[key]
                for key in (
                    "tokens",
                    "token_telemetry_complete",
                    "p50_seconds",
                    "p95_seconds",
                    "cost_usd",
                )
            }
            for name, value in zip(("baseline", "candidate"), summaries, strict=True)
        },
        "failure_transitions": dict(
            Counter(
                _failure_label(a) + "->" + _failure_label(b) for a, b in zip(old, new, strict=True)
            )
        ),
        "skill_delivery": {
            "scope": "worker_self_reported",
            "per_case": deliveries,
            "confirmed_cases": sum(item["status"] == "confirmed_response" for item in deliveries),
            "admission_refusal_cases": admission_refusals,
            "action_refusal_cases": [
                row["case_id"] for row in new if row["failure"] == "skill_action_not_allowed"
            ],
        },
        "blocked_by": blockers,
        "statistical_inference": "not_run",
        "independent_review": "not_run",
        "unseen_evaluation": "not_run",
        "repeated_pair_qualification": "not_run",
        "production_accepted": False,
    }
