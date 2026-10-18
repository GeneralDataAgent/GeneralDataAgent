"""Run each Stage 5 local action gate; never infer external business acceptance."""

import argparse
import json
import subprocess
from pathlib import Path

from generaldataagent.evaluation.stage_five import KINDS, run_business_regression
from generaldataagent.evaluation.stage_five_external import (
    QualifiedActionBundle,
    verify_action_qualification,
)
from generaldataagent.security.admission import load_release_keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument("--action-evidence-dir", type=Path)
    parser.add_argument("--trusted-signers", type=Path)
    parser.add_argument("--configuration-hashes", type=Path)
    parser.add_argument("--environment", choices=("staging", "production"), default="staging")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    local = run_business_regression(root / "evals/datasets/stage5-business.jsonl")
    external, blockers = [], []
    for kind in KINDS:
        gate = {
            "kind": kind,
            "status": "not_run",
            "blockers": ["independent_adapter_evidence_required"],
        }
        if args.action_evidence_dir is not None:
            try:
                if args.trusted_signers is None or args.configuration_hashes is None:
                    raise ValueError("trusted signers and pinned configuration hashes required")
                path = args.action_evidence_dir / f"{kind}.json"
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                    raise ValueError("missing or oversized external action evidence")
                raw_configs = args.configuration_hashes.read_bytes()
                if len(raw_configs) > 65536:
                    raise ValueError("configuration manifest too large")
                configs = json.loads(raw_configs)
                bundle = QualifiedActionBundle.model_validate_json(path.read_bytes())
                reasons = verify_action_qualification(
                    bundle,
                    kind=kind,
                    revision=revision,
                    configuration_hash=configs[kind],
                    environment=args.environment,
                    trusted_keys=load_release_keys(args.trusted_signers),
                )
                gate = {
                    "kind": kind,
                    "status": "failed" if reasons else "passed",
                    "blockers": list(reasons),
                }
            except Exception:
                gate = {"kind": kind, "status": "failed", "blockers": ["invalid_action_evidence"]}
        external.append(gate)
        if gate["status"] != "passed":
            blockers.append(f"missing_or_failed_external_{kind}_qualification")
    blockers += [
        "missing_production_cost_latency_comparison",
        "missing_independent_stage4_release_admission",
    ]
    if dirty:
        blockers.append("uncommitted_source_changes")
    if local["failed"]:
        blockers.append("local_business_regression_failed")
    report = {
        "stage": 5,
        "revision": revision,
        "development_gate_status": "passed" if not local["failed"] else "failed",
        "business_regression": local,
        "external_action_gates": external,
        "stage_acceptance_status": "incomplete",
        "blockers": blockers,
    }
    print(json.dumps(report, indent=2))
    return int(bool(local["failed"]) or args.require_production)


if __name__ == "__main__":
    raise SystemExit(main())
