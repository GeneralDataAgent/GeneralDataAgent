import base64
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.evaluation.stage_five import run_business_regression
from generaldataagent.evaluation.stage_five_external import (
    ActionQualification,
    QualifiedActionBundle,
    action_qualification_statement,
    verify_action_qualification,
)
from generaldataagent.security.release_bundle import ReleaseSignature

ROOT = Path(__file__).parents[1]
HASH = "sha256:" + "a" * 64
REVISION = "a" * 40


def signed_bundle():
    now = datetime.now(UTC)
    evidence = ActionQualification(
        kind="notification",
        revision=REVISION,
        configuration_hash=HASH,
        environment="staging",
        source_system="synthetic-test-receiver",
        destination="test-room",
        owner="owner",
        observed_at=now - timedelta(minutes=1),
        valid_until=now + timedelta(hours=1),
        trials=20,
        duplicate_side_effects=0,
        content_mismatches=0,
        compensation_attempts=5,
        compensation_successes=5,
        complete_audit_records=20,
        security_regressions=0,
        identity_and_policy_rechecked=True,
        expiry_and_cancel_tested=True,
        independent_kill_switch_tested=True,
        rate_limit_tested=True,
        crash_reconciliation_tested=True,
        receiver_receipts_verified=True,
        native_authorization_verified=True,
        business_metric="manual_minutes_saved",
        baseline_value=100,
        measured_value=80,
        improvement_direction="decrease",
        required_improvement=10,
        underlying_evidence_hash=HASH,
    )
    keys = {
        ("security-reviewer", "security"): Ed25519PrivateKey.generate(),
        ("operations-reviewer", "operations"): Ed25519PrivateKey.generate(),
    }
    bundle = QualifiedActionBundle(
        evidence=evidence,
        signatures=tuple(
            ReleaseSignature(signer_id=name, role=role, signature="") for name, role in keys
        ),
    )
    statement = action_qualification_statement(bundle)
    bundle = bundle.model_copy(
        update={
            "signatures": tuple(
                ReleaseSignature(
                    signer_id=name,
                    role=role,
                    signature=base64.b64encode(key.sign(statement)).decode(),
                )
                for (name, role), key in keys.items()
            )
        }
    )
    return bundle, {identity: key.public_key() for identity, key in keys.items()}


def verify(bundle, keys, **kwargs):
    return verify_action_qualification(
        bundle,
        **(
            dict(
                kind="notification",
                revision=REVISION,
                configuration_hash=HASH,
                environment="staging",
                trusted_keys=keys,
            )
            | kwargs
        ),
    )


def test_action_qualification_exact_signature_configuration_and_revocation():
    bundle, keys = signed_bundle()
    assert verify(bundle, keys) == ()
    for change in (
        {"kind": "ticket"},
        {"revision": "b" * 40},
        {"configuration_hash": "sha256:" + "b" * 64},
        {"environment": "production"},
    ):
        assert "action_scope_or_version_mismatch" in verify(bundle, keys, **change)
    assert "independent_action_signatures_invalid" in verify(bundle, {})
    tampered = bundle.model_copy(
        update={"evidence": bundle.evidence.model_copy(update={"destination": "other-room"})}
    )
    assert "independent_action_signatures_invalid" in verify(tampered, keys)
    assert "action_evidence_expired_or_future" in verify(
        bundle, keys, now=datetime.now(UTC) + timedelta(days=2)
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"duplicate_side_effects": 1}, "action_safety_or_compensation_failed"),
        ({"content_mismatches": 1}, "action_safety_or_compensation_failed"),
        ({"compensation_successes": 4}, "action_safety_or_compensation_failed"),
        ({"security_regressions": 1}, "action_safety_or_compensation_failed"),
        ({"complete_audit_records": 19}, "action_safety_or_compensation_failed"),
        ({"native_authorization_verified": False}, "action_safety_or_compensation_failed"),
        ({"measured_value": 95}, "action_business_value_below_target"),
    ],
)
def test_action_qualification_rejects_each_failed_threshold(change, reason):
    bundle, keys = signed_bundle()
    changed = bundle.model_copy(update={"evidence": bundle.evidence.model_copy(update=change)})
    assert reason in verify(changed, keys)


def test_frozen_local_matrix_and_incomplete_production_cli(tmp_path):
    matrix = ROOT / "evals/datasets/stage5-business.jsonl"
    result = run_business_regression(matrix)
    assert (result["total"], result["passed"], result["failed"]) == (42, 42, 0)
    broken = tmp_path / "broken.jsonl"
    broken.write_text(matrix.read_text().splitlines()[0])
    with pytest.raises(ValueError, match="complete"):
        run_business_regression(broken)
    for strict, code in ((False, 0), (True, 1)):
        args = [sys.executable, "evals/run_stage5_acceptance.py"]
        if strict:
            args.append("--require-production")
        completed = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "LANGSMITH_TRACING": "false"},
            check=False,
        )
        assert completed.returncode == code, completed.stderr
        report = json.loads(completed.stdout)
        assert report["development_gate_status"] == "passed"
        assert report["stage_acceptance_status"] == "incomplete"
        assert len(report["external_action_gates"]) == 6
        assert all(g["status"] == "not_run" for g in report["external_action_gates"])
