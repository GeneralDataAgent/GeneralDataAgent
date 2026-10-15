import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.evaluation import (
    EXTERNAL_GATE_REQUIREMENTS,
    ExternalGateEvidence,
    load_external_gate_results,
    write_external_gate_manifest,
)

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_acceptance.py"
ATTESTER = Path(__file__).resolve().parents[1] / "evals" / "attest_external_evidence.py"


def _write_bundle(root: Path, *, now: datetime) -> None:
    root.mkdir()
    for gate_id in EXTERNAL_GATE_REQUIREMENTS:
        evidence_path = root / f"{gate_id}.evidence.json"
        evidence_path.write_text(json.dumps({"gate_id": gate_id, "result": "passed"}))
        digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        manifest = ExternalGateEvidence(
            gate_id=gate_id,
            status="passed",
            source_system=f"test-{gate_id}",
            environment="non_production",
            verifier="independent-test-verifier",
            summary="Synthetic fixture exercising evidence ingestion only.",
            evidence_path=evidence_path.name,
            evidence_hash=f"sha256:{digest}",
            observed_at=now - timedelta(minutes=1),
            valid_until=now + timedelta(minutes=5),
        )
        write_external_gate_manifest(root / f"{gate_id}.manifest.json", manifest)


def _attest_bundle(root: Path, trusted_verifiers_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    private_key_path = root.parent / "verifier-private-key.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    trusted_verifiers_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "verifiers": [
                    {
                        "schema_version": "1.0",
                        "verifier": "independent-test-verifier",
                        "key_id": "test-key-1",
                        "algorithm": "ed25519",
                        "public_key": f"base64:{public_key}",
                        "allowed_gates": list(EXTERNAL_GATE_REQUIREMENTS),
                    }
                ],
            }
        )
    )
    attest = run_path(ATTESTER)["attest_external_evidence"]
    for gate_id in EXTERNAL_GATE_REQUIREMENTS:
        attest(
            manifest_path=root / f"{gate_id}.manifest.json",
            private_key_path=private_key_path,
            key_id="test-key-1",
        )


def test_valid_external_evidence_can_complete_stage(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(evidence_dir, trusted_verifiers)
    run_report = run_path(RUNNER)["run_acceptance_report"]

    report = run_report(evidence_dir, now=now, trusted_verifiers_path=trusted_verifiers)

    assert report["stage_acceptance_status"] == "passed"
    assert report["stage_acceptance_complete"] is True
    assert all(gate["status"] == "passed" for gate in report["external_gates"])
    assert all("attestation" in gate for gate in report["external_gates"])


def test_acceptance_rejects_external_evidence_without_trust_registry(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    run_report = run_path(RUNNER)["run_acceptance_report"]

    with pytest.raises(ValueError, match="trusted verifier registry"):
        run_report(evidence_dir, now=now)


def test_tampered_attested_manifest_fails_closed(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(evidence_dir, trusted_verifiers)
    manifest_path = evidence_dir / "real_dbt_project.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"] = "Tampered after independent attestation."
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="manifest hash mismatch"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=trusted_verifiers,
        )


def test_verifier_cannot_sign_an_unauthorized_gate(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(evidence_dir, trusted_verifiers)
    registry = json.loads(trusted_verifiers.read_text())
    registry["verifiers"][0]["allowed_gates"] = ["authoritative_profiler"]
    trusted_verifiers.write_text(json.dumps(registry))

    with pytest.raises(ValueError, match="not authorized for real_dbt_project"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=trusted_verifiers,
        )


def test_external_evidence_hash_mismatch_fails_closed(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    evidence_path = evidence_dir / "real_dbt_project.evidence.json"
    evidence_path.write_text("tampered")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_external_gate_results(evidence_dir, now=now)


def test_expired_external_evidence_cannot_complete_gate(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)

    results = load_external_gate_results(evidence_dir, now=now + timedelta(minutes=10))

    assert all(result["status"] == "expired" for result in results)


def test_external_evidence_path_cannot_escape_bundle() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="inside its bundle"):
        ExternalGateEvidence(
            gate_id="real_dbt_project",
            status="passed",
            source_system="dbt-cloud",
            environment="non_production",
            verifier="release-reviewer",
            summary="Rejected traversal fixture.",
            evidence_path="../outside.json",
            evidence_hash=f"sha256:{'0' * 64}",
            observed_at=now,
            valid_until=now + timedelta(minutes=5),
        )
