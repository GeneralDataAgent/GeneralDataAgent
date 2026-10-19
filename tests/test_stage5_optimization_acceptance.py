"""Ephemeral signed synthetic proofs test the gate, never establish real acceptance."""

import base64
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_stage5_acceptance import signed_bundle

from generaldataagent.evaluation.stage_five import KINDS
from generaldataagent.evaluation.stage_five_external import action_qualification_statement
from generaldataagent.evaluation.stage_five_optimization import (
    OptimizationBundle,
    OptimizationEvidence,
    optimization_statement,
    verify_optimization,
)
from generaldataagent.security.release import REQUIRED_EXERCISES, ReleaseEvidence, SLOEvidence
from generaldataagent.security.release_bundle import (
    ReleaseBundle,
    ReleaseSignature,
    release_statement,
)

_spec = importlib.util.spec_from_file_location(
    "stage5_cli", Path(__file__).parents[1] / "evals/run_stage5_acceptance.py"
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

REVISION = "a" * 40
HASH = "sha256:" + "a" * 64


def keys():
    return {
        ("fixture-security", "security"): Ed25519PrivateKey.generate(),
        ("fixture-operations", "operations"): Ed25519PrivateKey.generate(),
    }


def sign(bundle, statement, private):
    return bundle.model_copy(
        update={
            "signatures": tuple(
                ReleaseSignature(
                    signer_id=name,
                    role=role,
                    signature=base64.b64encode(key.sign(statement(bundle))).decode(),
                )
                for (name, role), key in private.items()
            )
        }
    )


def optimization(private):
    now = datetime.now(UTC)
    evidence = OptimizationEvidence(
        revision=REVISION,
        configuration_hash=HASH,
        dataset_hash=HASH,
        environment="staging",
        source_system="synthetic-test-only",
        observed_at=now - timedelta(minutes=1),
        valid_until=now + timedelta(hours=1),
        task_count=100,
        baseline_passed_tasks=90,
        optimized_passed_tasks=92,
        minimum_success_rate=0.9,
        baseline_cost_microunits=1000,
        optimized_cost_microunits=700,
        baseline_p95_ms=1000,
        optimized_p95_ms=800,
        minimum_cost_reduction_fraction=0.2,
        minimum_latency_reduction_fraction=0.1,
        safety_regressions=0,
        measured_billing_verified=True,
        cache_authorization_verified=True,
        task_budget_verified=True,
        underlying_evidence_hash=HASH,
    )
    placeholder = tuple(ReleaseSignature(signer_id=n, role=r, signature="") for n, r in private)
    return sign(
        OptimizationBundle(evidence=evidence, signatures=placeholder),
        optimization_statement,
        private,
    )


def verify(bundle, private, **kwargs):
    return verify_optimization(
        bundle,
        **(
            dict(
                revision=REVISION,
                configuration_hash=HASH,
                dataset_hash=HASH,
                environment="staging",
                trusted_keys={identity: key.public_key() for identity, key in private.items()},
            )
            | kwargs
        ),
    )


def test_signed_optimization_requires_exact_scope_quality_billing_and_targets():
    private = keys()
    bundle = optimization(private)
    assert verify(bundle, private) == ()
    for field in ("revision", "configuration_hash", "dataset_hash", "environment"):
        wrong = (
            "production"
            if field == "environment"
            else ("b" * 40 if field == "revision" else "sha256:" + "b" * 64)
        )
        assert "optimization_scope_or_version_mismatch" in verify(bundle, private, **{field: wrong})
    assert "optimization_evidence_expired_or_future" in verify(
        bundle, private, now=datetime.now(UTC) + timedelta(days=2)
    )
    assert "independent_optimization_signatures_invalid" in verify(bundle, {})
    for updates in (
        {"safety_regressions": 1},
        {"measured_billing_verified": False},
        {"optimized_passed_tasks": 89},
        {"optimized_passed_tasks": 101},
    ):
        changed = bundle.model_copy(update={"evidence": bundle.evidence.model_copy(update=updates)})
        changed = sign(changed, optimization_statement, private)
        assert "optimization_quality_safety_or_measurement_failed" in verify(changed, private)
    for updates in (
        {"optimized_cost_microunits": 900},
        {"optimized_p95_ms": 950},
        {"minimum_cost_reduction_fraction": 0, "minimum_latency_reduction_fraction": 0},
    ):
        changed = bundle.model_copy(update={"evidence": bundle.evidence.model_copy(update=updates)})
        changed = sign(changed, optimization_statement, private)
        assert "optimization_target_not_met" in verify(changed, private)
    with pytest.raises(ValueError):
        OptimizationEvidence.model_validate(
            {**bundle.evidence.model_dump(), "optimized_p95_ms": float("nan")}
        )


def test_complete_gate_cli_can_pass_only_with_all_signed_proofs(tmp_path, monkeypatch, capsys):
    private = keys()
    external = tmp_path / "external"
    external.mkdir()
    action, _ = signed_bundle()
    for kind in KINDS:
        evidence = action.evidence.model_copy(update={"kind": kind, "revision": REVISION})
        bundle = sign(
            action.model_copy(update={"evidence": evidence}),
            action_qualification_statement,
            private,
        )
        (external / f"{kind}.json").write_text(bundle.model_dump_json())
    config = tmp_path / "configs.json"
    config.write_text(json.dumps({kind: HASH for kind in KINDS}))
    trust = tmp_path / "trust.json"
    trust.write_text(
        json.dumps(
            [
                {
                    "signer_id": name,
                    "role": role,
                    "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
                }
                for (name, role), key in private.items()
            ]
        )
    )
    comparison = tmp_path / "comparison.json"
    comparison.write_text(optimization(private).model_dump_json())
    now = datetime.now(UTC)
    release = ReleaseBundle(
        environment="staging",
        valid_until=now + timedelta(hours=1),
        evidence=ReleaseEvidence(
            REVISION,
            now,
            100,
            90,
            0,
            0,
            0,
            100,
            100,
            REQUIRED_EXERCISES,
            slo=SLOEvidence(30 * 86400, 0, 100, 2.0, 60.0, 14),
        ),
        signatures=tuple(ReleaseSignature(signer_id=n, role=r, signature="") for n, r in private),
    )
    release_path = tmp_path / "release.json"
    release_path.write_text(sign(release, release_statement, private).model_dump_json())
    argv = [
        "run",
        "--require-production",
        "--action-evidence-dir",
        str(external),
        "--configuration-hashes",
        str(config),
        "--trusted-signers",
        str(trust),
        "--optimization-evidence",
        str(comparison),
        "--routing-configuration-hash",
        HASH,
        "--evaluation-dataset-hash",
        HASH,
        "--stage4-release-bundle",
        str(release_path),
    ]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(
        cli.subprocess,
        "check_output",
        lambda args, **kwargs: REVISION if args[-1] == "HEAD" else "",
    )
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stage_acceptance_status"] == "passed" and result["blockers"] == []
    # Real clean-tree state, every action and both proof groups are necessary, not optional waivers.
    (external / "notification.json").unlink()
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["stage_acceptance_status"] == "incomplete"
    comparison.write_text("{}")
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["optimization_gate"]["status"] == "failed"
