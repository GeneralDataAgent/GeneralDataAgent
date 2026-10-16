"""Aggregate local Stage 3 evidence and the signed hardened-sandbox review."""

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from generaldataagent.evaluation import (
    load_hardened_sandbox_gate,
    run_stage_three_evaluation,
)

EVAL_DIR = Path(__file__).parent


def run_acceptance_report(
    external_evidence_dir: Path | None = None,
    *,
    trusted_reviewers_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if external_evidence_dir is not None and trusted_reviewers_path is None:
        raise ValueError("external sandbox evidence requires trusted reviewers")
    with tempfile.TemporaryDirectory(prefix="gda-stage3-acceptance-") as directory:
        local = run_stage_three_evaluation(EVAL_DIR / "datasets" / "stage3.jsonl", Path(directory))
    local_gate = {
        "gate_id": "frozen_workbench_matrix",
        "status": "passed" if local.failed == 0 else "failed",
        "evidence": local.model_dump(mode="json"),
    }
    external_gate = load_hardened_sandbox_gate(
        external_evidence_dir,
        trusted_reviewers_path=trusted_reviewers_path,
        now=now,
    )
    complete = local_gate["status"] == "passed" and external_gate["status"] == "passed"
    return {
        "stage": 3,
        "development_gate_status": local_gate["status"],
        "stage_acceptance_status": "passed" if complete else "incomplete",
        "stage_acceptance_complete": complete,
        "development_gates": [local_gate],
        "external_gates": [external_gate],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-evidence-dir", type=Path)
    parser.add_argument("--trusted-reviewers", type=Path)
    args = parser.parse_args()
    report = run_acceptance_report(
        args.external_evidence_dir,
        trusted_reviewers_path=args.trusted_reviewers,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.external_evidence_dir is None:
        succeeded = report["development_gate_status"] == "passed"
    else:
        succeeded = report["stage_acceptance_complete"]
    raise SystemExit(0 if succeeded else 1)
