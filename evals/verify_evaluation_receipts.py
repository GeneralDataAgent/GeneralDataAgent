"""Read back evaluation receipts from stored reports without calling a model.

Every receipt-bearing row must resolve to a readable artifact whose exact bytes
reproduce the recorded result hash. Legacy rows without adapter metadata stay
readable and contribute no receipts. The runner never rewrites the report and
never scores anything itself; it only reports what the stored evidence supports.
"""

import argparse
import hashlib
import json
from pathlib import Path

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.evaluation.receipts import validate_evaluation_receipts
from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        nargs="+",
        required=True,
        help="Content tree holding the per-task evaluation artifacts",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a fresh readback record")

    report = json.loads(args.report.read_text())
    stores = [LocalArtifactStore(path) for path in args.artifact_root]

    def read_bytes(ref):
        for store in stores:
            try:
                return store.read_bytes(ref)
            except FileNotFoundError:
                continue
        raise FileNotFoundError("evaluation receipt artifact is unavailable")

    results = []
    receipt_errors = []
    step_receipts = 0
    for row in report["results"]:
        try:
            receipts = validate_evaluation_receipts(row, read_bytes=read_bytes)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError) as exc:
            receipt_errors.append(row["case_id"] + ":invalid_execution_receipt")
            results.append(
                {
                    "case_id": row["case_id"],
                    "receipt_steps": 0,
                    "verified_bytes": False,
                    "reason": type(exc).__name__,
                }
            )
            continue
        step_receipts += len(receipts)
        results.append(
            {
                "case_id": row["case_id"],
                "receipt_steps": len(receipts),
                "verified_bytes": True,
                "legacy_row": not receipts,
            }
        )

    errors = answer_receipt_errors(report, read_bytes=read_bytes)
    if any(not entry["verified_bytes"] for entry in results):
        errors.append("receipt_readback_incomplete")

    output = {
        "benchmark": report["benchmark"],
        "report_sha256": hashlib.sha256(args.report.read_bytes()).hexdigest(),
        "n": len(report["results"]),
        "step_receipts": step_receipts,
        "receipt_errors": receipt_errors,
        "stage7_errors": errors,
        "results": results,
        "new_model_calls": 0,
        "production_accepted": False,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    summary_keys = (
        "benchmark",
        "n",
        "step_receipts",
        "receipt_errors",
        "stage7_errors",
    )
    print(json.dumps({key: output[key] for key in summary_keys}, indent=2))


if __name__ == "__main__":
    main()
