import base64
import hashlib
import json
import sqlite3
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
    record_external_gate_acceptance,
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


def _attest_bundle(
    root: Path,
    trusted_verifiers_path: Path,
    *,
    signed_at: datetime,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    revoked_at: datetime | None = None,
) -> None:
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
                        "valid_from": (valid_from or signed_at - timedelta(days=1)).isoformat(),
                        "valid_until": (valid_until or signed_at + timedelta(days=1)).isoformat(),
                        "revoked_at": revoked_at.isoformat() if revoked_at else None,
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
            signed_at=signed_at,
        )


def _create_signer(
    root: Path,
    *,
    verifier: str,
    key_id: str,
    signed_at: datetime,
) -> tuple[dict[str, object], Path]:
    private_key = Ed25519PrivateKey.generate()
    private_key_path = root.parent / f"{key_id}.pem"
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
    return (
        {
            "schema_version": "1.0",
            "verifier": verifier,
            "key_id": key_id,
            "algorithm": "ed25519",
            "public_key": f"base64:{public_key}",
            "allowed_gates": list(EXTERNAL_GATE_REQUIREMENTS),
            "valid_from": (signed_at - timedelta(days=1)).isoformat(),
            "valid_until": (signed_at + timedelta(days=1)).isoformat(),
            "revoked_at": None,
        },
        private_key_path,
    )


def _attest_all_as(
    root: Path,
    *,
    verifier: str,
    key_id: str,
    private_key_path: Path,
    signed_at: datetime,
) -> None:
    attest = run_path(ATTESTER)["attest_external_evidence"]
    for gate_id in EXTERNAL_GATE_REQUIREMENTS:
        attest(
            manifest_path=root / f"{gate_id}.manifest.json",
            private_key_path=private_key_path,
            key_id=key_id,
            verifier=verifier,
            signed_at=signed_at,
        )


def test_valid_external_evidence_can_complete_stage(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(evidence_dir, trusted_verifiers, signed_at=now)
    evidence_ledger = tmp_path / "evidence-ledger.sqlite"
    run_report = run_path(RUNNER)["run_acceptance_report"]

    report = run_report(
        evidence_dir,
        now=now,
        trusted_verifiers_path=trusted_verifiers,
        evidence_ledger_path=evidence_ledger,
    )

    assert report["stage_acceptance_status"] == "passed"
    assert report["stage_acceptance_complete"] is True
    assert all(gate["status"] == "passed" for gate in report["external_gates"])
    assert all("attestation" in gate for gate in report["external_gates"])
    assert evidence_ledger.is_file()


def test_acceptance_rejects_external_evidence_without_ledger(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(evidence_dir, trusted_verifiers, signed_at=now)
    run_report = run_path(RUNNER)["run_acceptance_report"]

    with pytest.raises(ValueError, match="durable acceptance ledger"):
        run_report(evidence_dir, now=now, trusted_verifiers_path=trusted_verifiers)


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
    _attest_bundle(evidence_dir, trusted_verifiers, signed_at=now)
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
    _attest_bundle(evidence_dir, trusted_verifiers, signed_at=now)
    registry = json.loads(trusted_verifiers.read_text())
    registry["verifiers"][0]["allowed_gates"] = ["authoritative_profiler"]
    trusted_verifiers.write_text(json.dumps(registry))

    with pytest.raises(ValueError, match="not authorized for real_dbt_project"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=trusted_verifiers,
        )


def test_two_distinct_verifiers_satisfy_gate_thresholds(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    primary, primary_key = _create_signer(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="primary-key",
        signed_at=now,
    )
    secondary, secondary_key = _create_signer(
        evidence_dir,
        verifier="security-test-verifier",
        key_id="secondary-key",
        signed_at=now,
    )
    registry_path = tmp_path / "trusted-verifiers.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "verifiers": [primary, secondary],
                "gate_policies": [
                    {
                        "schema_version": "1.0",
                        "gate_id": gate_id,
                        "minimum_verifiers": 2,
                    }
                    for gate_id in EXTERNAL_GATE_REQUIREMENTS
                ],
            }
        )
    )
    _attest_all_as(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="primary-key",
        private_key_path=primary_key,
        signed_at=now,
    )
    _attest_all_as(
        evidence_dir,
        verifier="security-test-verifier",
        key_id="secondary-key",
        private_key_path=secondary_key,
        signed_at=now,
    )

    results = load_external_gate_results(
        evidence_dir,
        now=now,
        trusted_verifiers_path=registry_path,
    )

    assert all(len(result["attestations"]) == 2 for result in results)
    assert all("attestation" not in result for result in results)


def test_gate_threshold_fails_when_one_verifier_is_missing(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    primary, primary_key = _create_signer(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="primary-key",
        signed_at=now,
    )
    secondary, _ = _create_signer(
        evidence_dir,
        verifier="security-test-verifier",
        key_id="secondary-key",
        signed_at=now,
    )
    registry_path = tmp_path / "trusted-verifiers.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "verifiers": [primary, secondary],
                "gate_policies": [
                    {
                        "schema_version": "1.0",
                        "gate_id": "authoritative_profiler",
                        "minimum_verifiers": 2,
                    }
                ],
            }
        )
    )
    _attest_all_as(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="primary-key",
        private_key_path=primary_key,
        signed_at=now,
    )

    with pytest.raises(ValueError, match="threshold not met.*1/2"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=registry_path,
        )


def test_duplicate_verifier_keys_do_not_count_twice(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    first, first_key = _create_signer(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="first-key",
        signed_at=now,
    )
    second, second_key = _create_signer(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="second-key",
        signed_at=now,
    )
    registry_path = tmp_path / "trusted-verifiers.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "verifiers": [first, second],
                "gate_policies": [],
            }
        )
    )
    _attest_all_as(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="first-key",
        private_key_path=first_key,
        signed_at=now,
    )
    _attest_all_as(
        evidence_dir,
        verifier="independent-test-verifier",
        key_id="second-key",
        private_key_path=second_key,
        signed_at=now,
    )

    with pytest.raises(ValueError, match="duplicate external evidence verifier"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=registry_path,
        )


def test_evidence_ledger_is_idempotent_and_rejects_rollback(tmp_path) -> None:
    now = datetime.now(UTC)
    current_dir = tmp_path / "current"
    _write_bundle(current_dir, now=now)
    current_registry = tmp_path / "current-registry.json"
    _attest_bundle(current_dir, current_registry, signed_at=now)
    current_results = load_external_gate_results(
        current_dir,
        now=now,
        trusted_verifiers_path=current_registry,
    )
    ledger = tmp_path / "evidence-ledger.sqlite"

    record_external_gate_acceptance(current_results, ledger, accepted_at=now)
    record_external_gate_acceptance(current_results, ledger, accepted_at=now)
    with sqlite3.connect(ledger) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM external_gate_acceptance_history").fetchone()[
                0
            ]
            == 1
        )

    older = now - timedelta(minutes=10)
    older_dir = tmp_path / "older"
    _write_bundle(older_dir, now=older)
    older_registry = tmp_path / "older-registry.json"
    _attest_bundle(older_dir, older_registry, signed_at=older)
    older_results = load_external_gate_results(
        older_dir,
        now=older,
        trusted_verifiers_path=older_registry,
    )

    with pytest.raises(ValueError, match="rollback detected"):
        record_external_gate_acceptance(older_results, ledger, accepted_at=now)


def test_evidence_ledger_rejects_same_time_equivocation(tmp_path) -> None:
    now = datetime.now(UTC)
    first_dir = tmp_path / "first"
    _write_bundle(first_dir, now=now)
    first_registry = tmp_path / "first-registry.json"
    _attest_bundle(first_dir, first_registry, signed_at=now)
    first_results = load_external_gate_results(
        first_dir,
        now=now,
        trusted_verifiers_path=first_registry,
    )
    ledger = tmp_path / "evidence-ledger.sqlite"
    record_external_gate_acceptance(first_results, ledger, accepted_at=now)

    second_dir = tmp_path / "second"
    _write_bundle(second_dir, now=now)
    manifest_path = second_dir / "authoritative_profiler.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"] = "Different evidence with the same observation time."
    manifest_path.write_text(json.dumps(manifest))
    second_registry = tmp_path / "second-registry.json"
    _attest_bundle(second_dir, second_registry, signed_at=now)
    second_results = load_external_gate_results(
        second_dir,
        now=now,
        trusted_verifiers_path=second_registry,
    )

    with pytest.raises(ValueError, match="equivocation detected"):
        record_external_gate_acceptance(second_results, ledger, accepted_at=now)


def test_evidence_ledger_rejects_audit_chain_and_snapshot_tampering(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    registry = tmp_path / "registry.json"
    _attest_bundle(evidence_dir, registry, signed_at=now)
    results = load_external_gate_results(
        evidence_dir,
        now=now,
        trusted_verifiers_path=registry,
    )

    chain_ledger = tmp_path / "chain-ledger.sqlite"
    record_external_gate_acceptance(results, chain_ledger, accepted_at=now)
    with sqlite3.connect(chain_ledger) as connection:
        connection.execute(
            "UPDATE external_gate_acceptance_history SET bundle_hash = ? WHERE sequence = 1",
            (f"sha256:{'f' * 64}",),
        )
    with pytest.raises(ValueError, match="audit chain is invalid"):
        record_external_gate_acceptance(results, chain_ledger, accepted_at=now)

    snapshot_ledger = tmp_path / "snapshot-ledger.sqlite"
    record_external_gate_acceptance(results, snapshot_ledger, accepted_at=now)
    with sqlite3.connect(snapshot_ledger) as connection:
        connection.execute(
            "UPDATE external_gate_acceptance SET manifest_hash = ? "
            "WHERE gate_id = 'authoritative_profiler'",
            (f"sha256:{'e' * 64}",),
        )
    with pytest.raises(ValueError, match="snapshot does not match audit history"):
        record_external_gate_acceptance(results, snapshot_ledger, accepted_at=now)


def test_expired_rotated_key_accepts_evidence_signed_during_validity(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(
        evidence_dir,
        trusted_verifiers,
        signed_at=now - timedelta(minutes=1),
        valid_from=now - timedelta(days=2),
        valid_until=now - timedelta(seconds=1),
    )

    results = load_external_gate_results(
        evidence_dir,
        now=now,
        trusted_verifiers_path=trusted_verifiers,
    )

    assert all(result["status"] == "passed" for result in results)


def test_signature_before_key_activation_fails_closed(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(
        evidence_dir,
        trusted_verifiers,
        signed_at=now,
        valid_from=now + timedelta(minutes=1),
        valid_until=now + timedelta(days=1),
    )

    with pytest.raises(ValueError, match="outside key validity"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=trusted_verifiers,
        )


def test_revoked_signing_key_fails_closed_at_revocation_time(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(
        evidence_dir,
        trusted_verifiers,
        signed_at=now - timedelta(minutes=1),
        revoked_at=now,
    )

    with pytest.raises(ValueError, match="signing key is revoked"):
        load_external_gate_results(
            evidence_dir,
            now=now,
            trusted_verifiers_path=trusted_verifiers,
        )


def test_future_signature_time_fails_closed(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    trusted_verifiers = tmp_path / "trusted-verifiers.json"
    _attest_bundle(
        evidence_dir,
        trusted_verifiers,
        signed_at=now + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="signature time is invalid"):
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
