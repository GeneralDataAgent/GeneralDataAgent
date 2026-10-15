import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path

import pytest
from pydantic import ValidationError

from generaldataagent.evaluation import ProductionLoadTranscript, load_external_gate_results

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "qualify_production_scale_load.py"


def _write_transcript(
    path: Path,
    *,
    request_count: int = 2_000,
    recovery_probe_succeeded: bool = True,
) -> datetime:
    now = datetime.now(UTC)
    transcript = ProductionLoadTranscript(
        source_system="warehouse-load-harness",
        environment="staging",
        run_id="load-run-123",
        started_at=now - timedelta(minutes=5),
        completed_at=now - timedelta(minutes=1),
        request_count=request_count,
        max_workers=64,
        succeeded=request_count,
        failed=0,
        cancelled=0,
        artifact_count=request_count,
        target_requests_per_second=100,
        observed_requests_per_second=125,
        p50_request_ms=80,
        p95_request_ms=220,
        p99_request_ms=400,
        max_p95_ms=500,
        saturation_signal="connection_pool",
        saturation_observed=True,
        backpressure_activated=True,
        recovery_probe_succeeded=recovery_probe_succeeded,
        recovery_seconds=8,
        max_recovery_seconds=30,
    )
    path.write_text(transcript.model_dump_json())
    return now


def test_production_load_qualifier_emits_passing_evidence(tmp_path) -> None:
    transcript = tmp_path / "transcript.json"
    now = _write_transcript(transcript)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_production_scale_load"]

    result = qualify(
        transcript_path=transcript,
        output_dir=output,
        verifier="performance-reviewer",
        observed_at=now,
    )

    assert result["passed"] is True
    assert all(result["checks"].values())
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["production_scale_load"]["status"] == "passed"
    payload = json.loads((output / "production_scale_load.evidence.json").read_text())
    assert payload["criteria"]["minimum_request_count"] == 1_000
    assert str(tmp_path) not in json.dumps(payload)


def test_production_load_qualifier_preserves_failed_scale_and_recovery(tmp_path) -> None:
    transcript = tmp_path / "transcript.json"
    now = _write_transcript(
        transcript,
        request_count=500,
        recovery_probe_succeeded=False,
    )
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_production_scale_load"]

    result = qualify(
        transcript_path=transcript,
        output_dir=output,
        verifier="performance-reviewer",
        observed_at=now,
    )

    assert result["passed"] is False
    assert result["checks"]["production_scale"] is False
    assert result["checks"]["recovery_probe"] is False
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["production_scale_load"]["status"] == "failed"


def test_production_load_transcript_rejects_invalid_accounting_and_percentiles() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ProductionLoadTranscript(
            source_system="warehouse-load-harness",
            environment="staging",
            run_id="load-run-invalid",
            started_at=now - timedelta(minutes=1),
            completed_at=now,
            request_count=10,
            max_workers=2,
            succeeded=9,
            failed=0,
            cancelled=0,
            artifact_count=9,
            target_requests_per_second=5,
            observed_requests_per_second=6,
            p50_request_ms=10,
            p95_request_ms=30,
            p99_request_ms=20,
            max_p95_ms=50,
            saturation_signal="cpu",
            saturation_observed=True,
            backpressure_activated=True,
            recovery_probe_succeeded=True,
            recovery_seconds=1,
            max_recovery_seconds=5,
        )
