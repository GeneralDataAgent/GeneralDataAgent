import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path
from uuid import uuid4

from generaldataagent.evaluation import ExternalApprovalTranscript, load_external_gate_results

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "qualify_external_approval_service.py"


def _write_transcript(path: Path, *, replay_blocked: bool = True) -> datetime:
    now = datetime.now(UTC)
    transcript = ExternalApprovalTranscript(
        source_system="enterprise-approval",
        environment="staging",
        service_request_id="request-123",
        approval_id=uuid4(),
        request_fingerprint=f"sha256:{'1' * 64}",
        tenant_id="tenant-1",
        requester_id="requester",
        approver_id="reviewer",
        requested_at=now - timedelta(minutes=3),
        approved_at=now - timedelta(minutes=2),
        consumed_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        request_bound=True,
        tenant_bound=True,
        self_approval_rejected=True,
        changed_request_rejected=True,
        replay_blocked=replay_blocked,
    )
    path.write_text(transcript.model_dump_json())
    return now


def test_external_approval_qualifier_emits_passing_evidence(tmp_path) -> None:
    transcript = tmp_path / "transcript.json"
    now = _write_transcript(transcript)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_external_approval_service"]

    result = qualify(
        transcript_path=transcript,
        output_dir=output,
        verifier="security-reviewer",
        observed_at=now,
    )

    assert result["passed"] is True
    assert all(result["checks"].values())
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["external_approval_service"]["status"] == "passed"
    payload = json.loads((output / "external_approval_service.evidence.json").read_text())
    assert "tenant_id" not in payload
    assert str(tmp_path) not in json.dumps(payload)


def test_external_approval_qualifier_preserves_failed_replay_check(tmp_path) -> None:
    transcript = tmp_path / "transcript.json"
    now = _write_transcript(transcript, replay_blocked=False)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_external_approval_service"]

    result = qualify(
        transcript_path=transcript,
        output_dir=output,
        verifier="security-reviewer",
        observed_at=now,
    )

    assert result["passed"] is False
    assert result["checks"]["replay_blocked"] is False
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["external_approval_service"]["status"] == "failed"
