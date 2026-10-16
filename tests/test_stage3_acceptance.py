import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import ValidationError

from generaldataagent.evaluation import (
    HardenedSandboxReview,
    SandboxReviewAttestation,
    SandboxReviewAttestationStatement,
    StageThreeGateManifest,
    TrustedSandboxReviewerRegistry,
    load_hardened_sandbox_gate,
    serialize_sandbox_attestation,
)

NOW = datetime(2026, 10, 16, 8, tzinfo=UTC)


def _review(**overrides):
    values = {
        "source_system": "independent-sandbox-lab",
        "environment": "staging",
        "runtime_name": "stage3-python-sandbox",
        "image_digest": f"sha256:{'a' * 64}",
        "non_root_user": True,
        "read_only_root_filesystem": True,
        "network_default_deny": True,
        "syscall_filter": True,
        "no_new_privileges": True,
        "resource_limits_enforced": True,
        "secretless_workload": True,
        "arbitrary_code_rejected": True,
        "escape_tests_total": 24,
        "escape_tests_passed": 24,
        "reviewer": "security-reviewer-a",
        "reviewed_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return HardenedSandboxReview(**values)


def _bundle(root: Path):
    root.mkdir()
    review = _review()
    evidence_path = root / "hardened_sandbox_review.evidence.json"
    evidence_bytes = (
        json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    evidence_path.write_bytes(evidence_bytes)
    manifest = StageThreeGateManifest(
        source_system=review.source_system,
        verifier=review.reviewer,
        summary="Independent sandbox review passed all controls and escape tests.",
        evidence_path=evidence_path.name,
        evidence_hash=f"sha256:{hashlib.sha256(evidence_bytes).hexdigest()}",
        observed_at=review.reviewed_at,
        valid_until=NOW + timedelta(days=20),
    )
    manifest_path = root / "hardened_sandbox_review.manifest.json"
    manifest_bytes = (
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    manifest_path.write_bytes(manifest_bytes)

    private_keys = (Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate())
    reviewers = []
    for index, (verifier, key) in enumerate(
        zip(("security-reviewer-a", "platform-reviewer-b"), private_keys, strict=True), start=1
    ):
        key_id = f"review-key-{index}"
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        reviewers.append(
            {
                "verifier": verifier,
                "key_id": key_id,
                "public_key": f"base64:{base64.b64encode(public).decode()}",
                "valid_from": (NOW - timedelta(days=30)).isoformat(),
                "valid_until": (NOW + timedelta(days=30)).isoformat(),
            }
        )
        statement = SandboxReviewAttestationStatement(
            verifier=verifier,
            key_id=key_id,
            manifest_hash=f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
            signed_at=NOW - timedelta(hours=12),
        )
        attestation = SandboxReviewAttestation(
            **statement.model_dump(),
            signature=(
                "base64:"
                + base64.b64encode(key.sign(serialize_sandbox_attestation(statement))).decode()
            ),
        )
        (root / f"hardened_sandbox_review.{key_id}.attestation.json").write_text(
            json.dumps(attestation.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
    registry_path = root.parent / "trusted-reviewers.json"
    registry_path.write_text(
        json.dumps({"reviewers": reviewers, "minimum_reviewers": 2}, indent=2) + "\n"
    )
    return evidence_path, registry_path


def test_hardened_sandbox_gate_requires_signed_multi_party_review(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _, registry = _bundle(root)
    result = load_hardened_sandbox_gate(root, trusted_reviewers_path=registry, now=NOW)
    assert result["status"] == "passed"
    assert result["reviewers"] == ["security-reviewer-a", "platform-reviewer-b"]
    assert result["escape_tests"] == 24


def test_hardened_sandbox_gate_rejects_missing_second_reviewer(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _, registry = _bundle(root)
    (root / "hardened_sandbox_review.review-key-2.attestation.json").unlink()
    with pytest.raises(ValueError, match="threshold not met"):
        load_hardened_sandbox_gate(root, trusted_reviewers_path=registry, now=NOW)


def test_hardened_sandbox_gate_rejects_tampered_evidence(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    evidence, registry = _bundle(root)
    evidence.write_text("{}")
    with pytest.raises(ValueError, match="evidence hash mismatch"):
        load_hardened_sandbox_gate(root, trusted_reviewers_path=registry, now=NOW)


def test_hardened_sandbox_review_fails_if_any_control_is_false() -> None:
    with pytest.raises(ValidationError, match="every security control"):
        _review(network_default_deny=False)


def test_trusted_reviewer_registry_requires_distinct_reviewers() -> None:
    key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    reviewer = {
        "verifier": "same-reviewer",
        "key_id": "one",
        "public_key": f"base64:{base64.b64encode(key).decode()}",
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
    }
    with pytest.raises(ValidationError, match="distinct trusted reviewers"):
        TrustedSandboxReviewerRegistry(
            reviewers=(reviewer, reviewer | {"key_id": "two"}),
            minimum_reviewers=2,
        )


def test_stage_three_acceptance_is_incomplete_without_external_review() -> None:
    runner = run_path(Path(__file__).parents[1] / "evals" / "run_stage3_acceptance.py")
    report = runner["run_acceptance_report"]()
    assert report["development_gate_status"] == "passed"
    assert report["stage_acceptance_status"] == "incomplete"
    assert report["external_gates"][0]["status"] == "not_run"


def test_qualify_attest_and_acceptance_flow(tmp_path: Path) -> None:
    eval_dir = Path(__file__).parents[1] / "evals"
    qualifier = run_path(eval_dir / "qualify_stage3_sandbox.py")["qualify_sandbox_review"]
    attester = run_path(eval_dir / "attest_stage3_sandbox.py")["attest_sandbox_review"]
    acceptance = run_path(eval_dir / "run_stage3_acceptance.py")["run_acceptance_report"]
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_review().model_dump(mode="json")))
    bundle = tmp_path / "qualified"
    result = qualifier(review_path=review_path, output_dir=bundle)
    manifest_path = Path(result["manifest_path"])
    reviewers = []
    for index, verifier in enumerate(("security-reviewer-a", "platform-reviewer-b"), start=1):
        private_key = Ed25519PrivateKey.generate()
        key_path = tmp_path / f"key-{index}.pem"
        key_path.write_bytes(
            private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        )
        key_id = f"external-{index}"
        attester(
            manifest_path=manifest_path,
            private_key_path=key_path,
            key_id=key_id,
            verifier=verifier,
            signed_at=NOW - timedelta(hours=12),
        )
        public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        reviewers.append(
            {
                "verifier": verifier,
                "key_id": key_id,
                "public_key": f"base64:{base64.b64encode(public).decode()}",
                "valid_from": (NOW - timedelta(days=30)).isoformat(),
                "valid_until": (NOW + timedelta(days=30)).isoformat(),
            }
        )
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"reviewers": reviewers, "minimum_reviewers": 2}))
    report = acceptance(bundle, trusted_reviewers_path=registry, now=NOW)
    assert report["stage_acceptance_complete"] is True
    assert report["stage_acceptance_status"] == "passed"
