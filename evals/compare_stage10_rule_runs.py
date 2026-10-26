"""Compare two complete rule-Supervisor runs without merging their results.

Two runs of the same commit on the same denominator are not a paired experiment:
the model is stochastic, so a difference in role sequence is an observation about
that run, not an effect of any change. This tool records that honestly — identity
fields that agree, rows that differ, and both scores kept separate — instead of
producing a single combined pass rate.

It refuses to compare runs whose planned task ids, dataset, model route or annex
semantics differ, because those runs are not comparable at all, and it refuses an
incomplete run, because a partial denominator would make the comparison look
better or worse than what happened.
"""

import argparse
import hashlib
import json
from pathlib import Path

# Identity that must match for two runs to be worth comparing at all. The runner
# and diagnostics hashes are recorded but not required to match: the runner may
# have gained a report field, and the classifier is exactly what a re-run tests.
_REQUIRED_IDENTITY = (
    "planned_task_ids",
    "dataset_hashes",
    "model_route",
    "annex_semantics",
)
_RECORDED_IDENTITY = (
    "runner_hash",
    "supervisor_hash",
    "role_controller_hash",
    "completion_hash",
    "diagnostics_hash",
    "factory_hash",
    "document_context_hash",
    "governed_runner_hash",
    "document_extension",
    "document_configuration_hash",
)


class NotComparable(ValueError):
    """The two reports do not describe the same measurement."""


def _identity(report: dict, label: str) -> dict:
    if report.get("run_status") != "completed":
        raise NotComparable(f"{label} did not complete: {report.get('run_status')!r}")
    summary = report.get("summary") or {}
    if summary.get("pending_n") != 0 or summary.get("processed_n") != summary.get("n"):
        raise NotComparable(f"{label} has an incomplete denominator")
    return {key: report.get(key) for key in _REQUIRED_IDENTITY}


def _rows(report: dict) -> dict[str, dict]:
    return {str(row.get("task_id")): row for row in report["results"]}


def compare(baseline: dict, candidate: dict) -> dict:
    left, right = _identity(baseline, "baseline"), _identity(candidate, "candidate")
    differing = sorted(key for key in _REQUIRED_IDENTITY if left[key] != right[key])
    if differing:
        raise NotComparable(f"identity differs: {', '.join(differing)}")

    rows = _rows(baseline), _rows(candidate)
    if set(rows[0]) != set(rows[1]):
        raise NotComparable("the two runs cover different task ids")
    changes = []
    for task_id in sorted(rows[0], key=int):
        before, after = rows[0][task_id], rows[1][task_id]
        observed = {
            key: [before.get(key), after.get(key)]
            for key in ("outcome", "status", "delegated_roles", "roles_complete", "passed")
            if before.get(key) != after.get(key)
        }
        if observed:
            changes.append(
                {
                    "task_id": task_id,
                    "baseline_code": (before.get("stop_diagnostic") or {}).get("code"),
                    "candidate_code": (after.get("stop_diagnostic") or {}).get("code"),
                    "differences": observed,
                }
            )
    return {
        "comparable": True,
        "identity": {
            "required": {key: left[key] for key in _REQUIRED_IDENTITY},
            "recorded": {
                key: {
                    "baseline": baseline.get(key),
                    "candidate": candidate.get(key),
                    "same": baseline.get(key) == candidate.get(key),
                }
                for key in _RECORDED_IDENTITY
            },
        },
        # Each run keeps its own score. A paired difference is not computed:
        # these are two samples of a stochastic system, not a treatment effect.
        "scores": {
            "baseline": {
                "n": baseline["summary"]["n"],
                "passed": baseline["summary"]["passed_over_all_questions"],
                "model_calls": baseline["summary"].get("model_calls"),
                "usage": baseline["summary"].get("usage"),
            },
            "candidate": {
                "n": candidate["summary"]["n"],
                "passed": candidate["summary"]["passed_over_all_questions"],
                "model_calls": candidate["summary"].get("model_calls"),
                "usage": candidate["summary"].get("usage"),
            },
        },
        "changed_tasks": changes,
        "stable_tasks": sorted((set(rows[0]) - {change["task_id"] for change in changes}), key=int),
        "paired_effect_claimed": False,
        "production_accepted": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a fresh comparison record")
    if args.output.resolve() in (args.baseline.resolve(), args.candidate.resolve()):
        parser.error("output must not be one of the compared reports")

    output = {
        "baseline": args.baseline.name,
        "candidate": args.candidate.name,
        "report_sha256": {
            "baseline": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
            "candidate": hashlib.sha256(args.candidate.read_bytes()).hexdigest(),
        },
        **compare(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text())),
        "new_model_calls": 0,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
