"""Validate an independent hardened-sandbox review and create a hashed evidence manifest."""

import argparse
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from generaldataagent.evaluation import HardenedSandboxReview, StageThreeGateManifest


def qualify_sandbox_review(*, review_path: Path, output_dir: Path) -> dict[str, object]:
    review = HardenedSandboxReview.model_validate_json(
        review_path.resolve(strict=True).read_bytes()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_name = "hardened_sandbox_review.evidence.json"
    evidence_path = output_dir / evidence_name
    manifest_path = output_dir / "hardened_sandbox_review.manifest.json"
    if evidence_path.exists() or manifest_path.exists():
        raise FileExistsError("hardened sandbox evidence bundle already exists")
    evidence_bytes = (
        json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    evidence_path.write_bytes(evidence_bytes)
    evidence_hash = f"sha256:{hashlib.sha256(evidence_bytes).hexdigest()}"
    manifest = StageThreeGateManifest(
        source_system=review.source_system,
        verifier=review.reviewer,
        summary=(
            f"Independent review passed {review.escape_tests_passed}/"
            f"{review.escape_tests_total} escape tests for {review.runtime_name}."
        ),
        evidence_path=evidence_name,
        evidence_hash=evidence_hash,
        observed_at=review.reviewed_at,
        valid_until=review.reviewed_at + timedelta(days=30),
    )
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    return {
        "gate_id": manifest.gate_id,
        "evidence_path": str(evidence_path),
        "manifest_path": str(manifest_path),
        "evidence_hash": evidence_hash,
        "status": "awaiting_attestations",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            qualify_sandbox_review(review_path=args.review, output_dir=args.output_dir),
            indent=2,
            sort_keys=True,
        )
    )
