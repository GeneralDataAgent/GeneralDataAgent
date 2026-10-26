"""Recompute rule-stop diagnostics from a stored report without rewriting it.

A stored report's ``stop_diagnostics`` records the classifier as it stood when the
run finished. When the classifier is corrected, the stored summary keeps the old
code and disagrees with the row it summarizes. This runner recomputes every row
with the current classifier and reports the differences, so a review does not
have to rely on an ad-hoc script and the original evidence is never edited.

It calls no model, changes no score and writes a new file only.
"""

import argparse
import hashlib
import json
from pathlib import Path

from generaldataagent.evaluation.rule_diagnostics import stop_diagnostic, summarize_rule_run


def _stored_code(row: dict) -> str | None:
    stored = row.get("stop_diagnostic")
    return stored.get("code") if isinstance(stored, dict) else None


def reclassify(report: dict) -> dict:
    results = report["results"]
    planned_n = report.get("summary", {}).get("n", len(results))
    changed = []
    for row in results:
        code = stop_diagnostic(row)["code"]
        stored = _stored_code(row)
        if stored != code:
            changed.append(
                {
                    "task_id": row.get("task_id", row.get("case_id")),
                    "outcome": row["outcome"],
                    "status": row.get("status"),
                    "stored": stored,
                    "recomputed": code,
                }
            )
    return {
        "n": planned_n,
        "processed_n": len(results),
        "stored_stop_diagnostics": report.get("summary", {}).get("stop_diagnostics"),
        "recomputed_stop_diagnostics": summarize_rule_run(results, planned_n=planned_n)[
            "stop_diagnostics"
        ],
        # A row without a stored code predates the field; it is listed rather than
        # assumed to have agreed with the current classifier.
        "changed_rows": changed,
        "production_accepted": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a fresh reclassification record")
    if args.output.resolve() == args.report.resolve():
        parser.error("output must not be the report being read")

    report = json.loads(args.report.read_text())
    output = {
        "report_sha256": hashlib.sha256(args.report.read_bytes()).hexdigest(),
        "report": args.report.name,
        **reclassify(report),
        "new_model_calls": 0,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
