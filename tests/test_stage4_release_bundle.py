import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from generaldataagent.security.release import REQUIRED_EXERCISES, ReleaseEvidence, SLOEvidence
from generaldataagent.security.release_bundle import (
    ReleaseBundle,
    ReleaseSignature,
    release_statement,
    verify_release_bundle,
)


def test_signed_release_requires_two_distinct_roles_keys_revision_and_environment():
    now = datetime.now(UTC)
    security, operations = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    evidence = ReleaseEvidence(
        "revision",
        now,
        100,
        88,
        0,
        0,
        0,
        100,
        99,
        REQUIRED_EXERCISES,
        slo=SLOEvidence(30 * 86400, 0, 100, 2.0, 60.0, 14),
    )
    signatures = (
        ReleaseSignature(signer_id="security", role="security", signature=""),
        ReleaseSignature(signer_id="operations", role="operations", signature=""),
    )
    bundle = ReleaseBundle(
        environment="staging",
        evidence=evidence,
        valid_until=now + timedelta(hours=1),
        signatures=signatures,
    )

    def sign(bundle):
        return bundle.model_copy(
            update={
                "signatures": tuple(
                    signature.model_copy(
                        update={
                            "signature": base64.b64encode(
                                key.sign(release_statement(bundle))
                            ).decode()
                        }
                    )
                    for signature, key in zip(
                        bundle.signatures, (security, operations), strict=True
                    )
                )
            }
        )

    bundle = sign(bundle)
    keys = {
        ("security", "security"): security.public_key(),
        ("operations", "operations"): operations.public_key(),
    }
    args = dict(revision="revision", environment="staging", trusted_keys=keys, now=now)
    assert verify_release_bundle(bundle, **args) == ()
    reloaded = ReleaseBundle.model_validate_json(bundle.model_dump_json())
    assert release_statement(reloaded) == release_statement(bundle)
    assert verify_release_bundle(reloaded, **args) == ()
    for changed in (
        bundle.model_copy(update={"environment": "production"}),
        bundle.model_copy(update={"valid_until": now - timedelta(seconds=1)}),
        bundle.model_copy(update={"signatures": (bundle.signatures[0],) * 2}),
        bundle.model_copy(
            update={
                "signatures": tuple(
                    sig.model_copy(update={"signature": "AAAA"}) for sig in bundle.signatures
                )
            }
        ),
    ):
        assert verify_release_bundle(changed, **args)
    assert verify_release_bundle(bundle, **{**args, "revision": "different"})
    assert verify_release_bundle(bundle, **{**args, "trusted_keys": {}})
    same_keys = {
        ("security", "security"): security.public_key(),
        ("operations", "operations"): security.public_key(),
    }
    repeated = bundle.model_copy(
        update={
            "signatures": tuple(
                sig.model_copy(
                    update={
                        "signature": base64.b64encode(
                            security.sign(release_statement(bundle))
                        ).decode()
                    }
                )
                for sig in bundle.signatures
            )
        }
    )
    assert verify_release_bundle(repeated, **{**args, "trusted_keys": same_keys})
    invalid = bundle.model_dump(mode="json")
    invalid["evidence"]["task_count"] = "100"
    with pytest.raises(ValueError):
        ReleaseBundle.model_validate_json(__import__("json").dumps(invalid))
