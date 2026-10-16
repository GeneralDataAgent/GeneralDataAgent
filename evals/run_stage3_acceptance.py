"""Aggregate local Stage 3 evidence and the signed hardened-sandbox review."""

import argparse
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from generaldataagent.contracts import OCIContainerRuntime, SandboxQuota
from generaldataagent.data_science import OCIContainerSandbox
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
    oci_gate = _oci_contract_probe()
    external_gate = load_hardened_sandbox_gate(
        external_evidence_dir,
        trusted_reviewers_path=trusted_reviewers_path,
        now=now,
    )
    development_gates = [local_gate, oci_gate]
    development_passed = all(gate["status"] == "passed" for gate in development_gates)
    complete = development_passed and external_gate["status"] == "passed"
    return {
        "stage": 3,
        "development_gate_status": "passed" if development_passed else "failed",
        "stage_acceptance_status": "passed" if complete else "incomplete",
        "stage_acceptance_complete": complete,
        "development_gates": development_gates,
        "external_gates": [external_gate],
    }


def _oci_contract_probe() -> dict[str, object]:
    digest = "sha256:" + "0" * 64
    runtime = OCIContainerRuntime(
        engine="docker",
        image_ref=f"registry.invalid/generaldataagent-stage3:1.0@{digest}",
    )
    command = OCIContainerSandbox(runtime).build_command(SandboxQuota())
    required = {
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--user=65532:65532",
    }
    forbidden_prefixes = ("--volume", "--mount", "--env", "--privileged")
    no_forbidden_flags = not any(item.startswith(forbidden_prefixes) for item in command)
    dockerfile = EVAL_DIR.parent / "infra" / "sandbox" / "stage3" / "Dockerfile"
    source = dockerfile.read_text()
    pinned_base = re.search(r"^FROM .*@sha256:[0-9a-f]{64}$", source, re.MULTILINE)
    non_root = "USER 65532:65532" in source
    passed = (
        required.issubset(command) and no_forbidden_flags and pinned_base is not None and non_root
    )
    return {
        "gate_id": "oci_isolation_contract",
        "status": "passed" if passed else "failed",
        "evidence": {
            "image_digest_required": runtime.image_ref.endswith(digest),
            "required_flags": sorted(required),
            "forbidden_mount_or_environment_flags": no_forbidden_flags,
            "base_image_pinned": pinned_base is not None,
            "non_root_image_user": non_root,
            "runtime_executed": False,
        },
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
