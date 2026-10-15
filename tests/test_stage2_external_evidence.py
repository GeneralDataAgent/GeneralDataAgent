import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path

import pytest

from generaldataagent.evaluation import (
    EXTERNAL_GATE_REQUIREMENTS,
    ExternalGateEvidence,
    load_external_gate_results,
    write_external_gate_manifest,
)

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_acceptance.py"


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


def test_valid_external_evidence_can_complete_stage(tmp_path) -> None:
    now = datetime.now(UTC)
    evidence_dir = tmp_path / "evidence"
    _write_bundle(evidence_dir, now=now)
    run_report = run_path(RUNNER)["run_acceptance_report"]

    report = run_report(evidence_dir, now=now)

    assert report["stage_acceptance_status"] == "passed"
    assert report["stage_acceptance_complete"] is True
    assert all(gate["status"] == "passed" for gate in report["external_gates"])


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
