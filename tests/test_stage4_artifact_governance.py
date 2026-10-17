from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactClassification
from generaldataagent.security.dlp import DLPDeniedError, TextDLPScanner


@pytest.mark.parametrize(
    "content,code",
    [
        (b'{"password":"verysecret123"}', "credential"),
        (b"contact alice@example.com", "email"),
        (b"mobile 13800138000", "phone"),
        (b"-----BEGIN PRIVATE KEY-----", "private_key"),
        (b"Bearer abcdefghijklmnopqrstuvwxyz", "bearer"),
        (b"\xff\xfe", "encoding"),
    ],
)
def test_sensitive_content_is_denied_without_echoing_values(content, code):
    with pytest.raises(DLPDeniedError, match=code) as error:
        TextDLPScanner().check(content)
    assert content.decode("utf-8", errors="replace") not in str(error.value)


def test_artifact_scope_retention_hold_export_and_restart(tmp_path):
    now = datetime.now(UTC)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))

    def store(who=actor):
        return GovernedArtifactStore(
            tmp_path / "content",
            index_path=tmp_path / "index.db",
            actor=who,
            retention_seconds=10,
            clock=lambda: now,
        )

    first = store()
    args = dict(task_id=uuid4(), artifact_type="report", created_by="reporter", extension="json")
    with pytest.raises(DLPDeniedError):
        first.put_bytes(**args, content=b'{"api_key":"abcdefgh123456"}')
    assert not list((tmp_path / "content").iterdir())
    ref = first.put_json(
        task_id=args["task_id"], artifact_type="report", value={"count": 20}, created_by="reporter"
    )
    held = first.put_bytes(
        **args, content=b"{}", classification=ArtifactClassification.CONFIDENTIAL
    )
    first.set_legal_hold(held, held=True)
    assert store().read_bytes(ref) == b'{"count":20}'
    assert first.export_bytes(ref) == b'{"count":20}'
    with pytest.raises(DLPDeniedError, match="classification"):
        first.export_bytes(held)
    for change in ({"tenant_id": "other"}, {"project_id": "other"}, {"data_domains": ("other",)}):
        foreign = store(actor.model_copy(update=change))
        with pytest.raises(PermissionError):
            foreign.read_bytes(ref)
        with pytest.raises(PermissionError):
            foreign.set_legal_hold(ref, held=False)
    with pytest.raises(PermissionError):
        first.read_bytes(ref.model_copy(update={"classification": "public"}))
    now += timedelta(seconds=10)
    with pytest.raises(PermissionError, match="expired"):
        first.read_bytes(ref)
    assert store(actor.model_copy(update={"tenant_id": "other"})).delete_expired() == ()
    assert first.delete_expired() == (ref.artifact_id,)
    assert first.delete_expired() == ()
    assert first._path(held).exists()
    first.set_legal_hold(held, held=False)
    assert first.delete_expired() == (held.artifact_id,)
    assert not first._path(held).exists()
    with pytest.raises(PermissionError):
        store().read_bytes(ref)


def test_artifact_configuration_and_hash_tampering_fail_closed(tmp_path):
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    with pytest.raises(ValueError):
        GovernedArtifactStore(tmp_path, index_path=tmp_path / "index", actor=actor)
    store = GovernedArtifactStore(tmp_path / "content", index_path=tmp_path / "index", actor=actor)
    ref = store.put_bytes(task_id=uuid4(), artifact_type="report", created_by="r", content=b"safe")
    store._path(ref).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        store.read_bytes(ref)
    with pytest.raises(DLPDeniedError, match="size"):
        TextDLPScanner(max_bytes=1).check(b"large")
