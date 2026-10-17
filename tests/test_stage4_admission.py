import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from generaldataagent.api.production import create_released_app
from generaldataagent.security.admission import FileReleaseAdmission
from generaldataagent.security.identity import SessionBroker, SQLiteSessionBroker
from generaldataagent.security.release import REQUIRED_EXERCISES, ReleaseEvidence, SLOEvidence
from generaldataagent.security.release_bundle import (
    ReleaseBundle,
    ReleaseSignature,
    release_statement,
)


def test_live_signed_gate_blocks_startup_revoked_trust_changed_source_and_new_work(tmp_path):
    now = datetime.now(UTC)
    keys = [
        ("security", "security", Ed25519PrivateKey.generate()),
        ("operations", "operations", Ed25519PrivateKey.generate()),
    ]
    evidence = ReleaseEvidence(
        "a" * 40,
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
    bundle = ReleaseBundle(
        environment="staging",
        evidence=evidence,
        valid_until=now + timedelta(hours=1),
        signatures=tuple(
            ReleaseSignature(signer_id=name, role=role, signature="") for name, role, _ in keys
        ),
    )
    bundle = bundle.model_copy(
        update={
            "signatures": tuple(
                sig.model_copy(
                    update={
                        "signature": base64.b64encode(key.sign(release_statement(bundle))).decode()
                    }
                )
                for sig, (_, _, key) in zip(bundle.signatures, keys, strict=True)
            )
        }
    )
    bundle_path, trust_path = tmp_path / "release", tmp_path / "trust"
    bundle_path.write_text(bundle.model_dump_json())
    records = [
        dict(
            signer_id=name,
            role=role,
            public_key=base64.b64encode(key.public_key().public_bytes_raw()).decode(),
        )
        for name, role, key in keys
    ]
    trust_path.write_text(json.dumps(records))
    admission = FileReleaseAdmission(
        bundle_path=bundle_path, trust_path=trust_path, revision="a" * 40, environment="staging"
    )
    assert admission.allowed(now=now)
    assert not admission.allowed(now=now + timedelta(hours=2))
    broker = SQLiteSessionBroker(tmp_path / "sessions")
    enabled = True
    app = create_released_app(
        admission=admission, session_broker=broker, service_enabled=lambda: enabled
    )
    client = TestClient(app)
    token = broker.issue(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "u",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "d",
    }
    assert client.get("/v1/capabilities", headers=headers).status_code == 200
    trust_path.write_text("[]")  # Revocation is observed on the next request, without restart.
    assert client.get("/v1/capabilities", headers=headers).status_code == 503
    assert client.get("/auth/login").status_code == 503
    assert client.get("/health").status_code == 200
    with pytest.raises(ValueError):
        create_released_app(
            admission=admission, session_broker=broker, service_enabled=lambda: True
        )
    trust_path.write_text(json.dumps(records))
    enabled = False
    assert client.get("/v1/capabilities", headers=headers).status_code == 503
    with pytest.raises(ValueError):
        create_released_app(
            admission=admission, session_broker=SessionBroker(), service_enabled=lambda: True
        )
    with pytest.raises(ValueError):
        create_released_app(
            admission=admission,
            session_broker=broker,
            service_enabled=lambda: True,
            production_mode=False,
        )
    with pytest.raises(ValueError):
        FileReleaseAdmission(
            bundle_path=bundle_path,
            trust_path=trust_path,
            revision="main",
            environment="production",
        )
