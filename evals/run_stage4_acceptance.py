"""Stage 4 local security baseline and independently signed production release gate."""

import argparse
import json
import subprocess
from pathlib import Path

from generaldataagent.evaluation.stage_four import run_security_regression
from generaldataagent.security.admission import load_release_keys
from generaldataagent.security.release_bundle import ReleaseBundle, verify_release_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-bundle", type=Path)
    parser.add_argument("--trusted-signers", type=Path)
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument("--environment", choices=("staging", "production"), default="staging")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()
    )
    local = run_security_regression(root / "evals/datasets/stage4-security.jsonl")
    blockers = ["missing_independent_release_evidence"]
    if args.release_bundle is not None and args.trusted_signers is not None:
        try:
            bundle = ReleaseBundle.model_validate_json(args.release_bundle.read_text())
            keys = load_release_keys(args.trusted_signers)
            blockers = list(
                verify_release_bundle(
                    bundle, revision=revision, environment=args.environment, trusted_keys=keys
                )
            )
        except Exception:
            blockers = ["invalid_release_evidence_or_trust_registry"]
    if dirty:
        blockers.append("uncommitted_source_changes")
    if local["failed"]:
        blockers.append("local_security_regression_failed")
    print(
        json.dumps(
            {
                "stage": 4,
                "revision": revision,
                "development_gate_status": "passed" if not local["failed"] else "failed",
                "security_regression": local,
                "stage_acceptance_status": "passed" if not blockers else "incomplete",
                "blockers": blockers,
            },
            indent=2,
        )
    )
    return int(bool(local["failed"]) or (args.require_production and bool(blockers)))


if __name__ == "__main__":
    raise SystemExit(main())
