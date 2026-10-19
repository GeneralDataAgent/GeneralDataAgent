"""Run each Stage 5 local action gate; never infer external business acceptance."""

import argparse
import json
import subprocess
from pathlib import Path

from generaldataagent.evaluation.stage_five import KINDS, run_business_regression
from generaldataagent.evaluation.stage_five_acceptance import assemble_stage_five_report
from generaldataagent.evaluation.stage_five_external import (
    QualifiedActionBundle,
    verify_action_qualification,
)
from generaldataagent.evaluation.stage_five_optimization import (
    OptimizationBundle,
    verify_optimization,
)
from generaldataagent.security.admission import load_release_keys
from generaldataagent.security.release_bundle import ReleaseBundle, verify_release_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument("--action-evidence-dir", type=Path)
    parser.add_argument("--trusted-signers", type=Path)
    parser.add_argument("--configuration-hashes", type=Path)
    parser.add_argument("--environment", choices=("staging", "production"), default="staging")
    parser.add_argument("--optimization-evidence", type=Path)
    parser.add_argument("--routing-configuration-hash")
    parser.add_argument("--evaluation-dataset-hash")
    parser.add_argument("--stage4-release-bundle", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    local = run_business_regression(root / "evals/datasets/stage5-business.jsonl")
    external = []
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
    optimization = {"status": "not_run", "blockers": ["independent_optimization_evidence_required"]}
    if args.optimization_evidence is not None:
        try:
            if (
                args.trusted_signers is None
                or args.routing_configuration_hash is None
                or args.evaluation_dataset_hash is None
            ):
                raise ValueError("pinned routing/dataset hashes and trust are required")
            raw = args.optimization_evidence.read_bytes()
            if len(raw) > 65536:
                raise ValueError("optimization proof too large")
            reasons = verify_optimization(
                OptimizationBundle.model_validate_json(raw),
                revision=revision,
                configuration_hash=args.routing_configuration_hash,
                dataset_hash=args.evaluation_dataset_hash,
                environment=args.environment,
                trusted_keys=load_release_keys(args.trusted_signers),
            )
            optimization = {"status": "failed" if reasons else "passed", "blockers": list(reasons)}
        except Exception:
            optimization = {"status": "failed", "blockers": ["invalid_optimization_evidence"]}
    release = {"status": "not_run", "blockers": ["independent_stage4_release_evidence_required"]}
    if args.stage4_release_bundle is not None:
        try:
            if args.trusted_signers is None:
                raise ValueError("release trust is required")
            raw = args.stage4_release_bundle.read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError("release proof too large")
            reasons = verify_release_bundle(
                ReleaseBundle.model_validate_json(raw),
                revision=revision,
                environment=args.environment,
                trusted_keys=load_release_keys(args.trusted_signers),
            )
            release = {"status": "failed" if reasons else "passed", "blockers": list(reasons)}
        except Exception:
            release = {"status": "failed", "blockers": ["invalid_stage4_release_evidence"]}
    report = assemble_stage_five_report(
        revision=revision,
        local=local,
        external=external,
        optimization=optimization,
        release=release,
        dirty=dirty,
    )
    print(json.dumps(report, indent=2))
    return int(
        report["development_gate_status"] != "passed"
        or (args.require_production and bool(report["blockers"]))
    )


if __name__ == "__main__":
    raise SystemExit(main())
