"""Check stored case failure labels against what the current loop can produce.

A report keeps the failure label its run assigned. After the loop's labelling
rules change, a stored row can name an ending the current code can no longer
reach — the label is then a record of an older rule, not of this one. This
checker re-derives which labels are reachable from each row's own trace and
lists the rows that are not, so a review does not depend on reading traces by
hand.

It reads a stored report only. It calls no model, changes no score, and never
writes to the report it was given.
"""

import argparse
import hashlib
import json
from pathlib import Path

# Labels that describe how the agent loop ended rather than what a later
# scoring step decided. Each is reachable only from one terminal shape.
_TERMINAL = {
    # The loop ended because the step's own action raised.
    "execution_or_model_error": "error",
    # The loop spent its steps without submitting.
    "step_budget": "open",
    # The provider call itself failed; the entry carries the exception name.
    "model_error": "error",
    "time_budget": "any",
    "cost_budget": "any",
    "token_budget": "any",
    "repeated_action": "any",
}


def _terminal(row: dict) -> str:
    """How the loop ended, read from its last trace entry alone."""
    trace = row.get("trace") or []
    if not trace or not isinstance(trace[-1], dict):
        return "unknown"
    last = trace[-1]
    if last.get("status") == "error":
        return "error"
    if last.get("status") in {"unsupported", "repeated", "repair_refused"}:
        return "stopped"
    if last.get("status") == "ok":
        return "open"
    return "unknown"


def unreachable_rows(report: dict) -> tuple[list[dict], list[dict]]:
    """Split rows into labels the current loop cannot produce and unverifiable ones.

    A row with no readable trace is not evidence that the label was reachable,
    so it is listed separately rather than silently agreeing with either rule.
    """
    contradicting, unverifiable = [], []
    for row in report.get("results") or ():
        stored = row.get("failure")
        required = _TERMINAL.get(stored)
        if required is None or required == "any":
            continue
        actual = _terminal(row)
        entry = {
            "case_id": row.get("case_id"),
            "stored": stored,
            "expected_terminal": required,
            "observed_terminal": actual,
            "last_status": (row.get("trace") or [{}])[-1].get("status"),
            "last_code": (row.get("trace") or [{}])[-1].get("code"),
        }
        if actual == "unknown":
            unverifiable.append(entry)
        elif actual != required:
            contradicting.append(entry)
    return contradicting, unverifiable


def check(report: dict) -> dict:
    contradicting, unverifiable = unreachable_rows(report)
    return {
        "benchmark": report.get("benchmark"),
        "n": report.get("summary", {}).get("n", len(report.get("results") or ())),
        "processed_n": len(report.get("results") or ()),
        # A label the current loop cannot reach from this row's own ending.
        "unreachable_rows": contradicting,
        # No readable trace: the label is unverified, not agreed with.
        "unverifiable_rows": unverifiable,
        "production_accepted": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a fresh label check record")
    if args.output.resolve() == args.report.resolve():
        parser.error("output must not be the report being read")

    raw = args.report.read_bytes()
    output = {
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "report": args.report.name,
        **check(json.loads(raw)),
        "new_model_calls": 0,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
