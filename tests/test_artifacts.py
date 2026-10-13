from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore


def test_round_trip_and_hash_verification(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = store.put_json(
        task_id=uuid4(),
        artifact_type="analysis_result",
        value={"metric": 42},
        created_by="data-analyst",
    )
    assert store.read_bytes(ref) == b'{"metric":42}'
    assert ref.content_hash.startswith("sha256:")


def test_rejects_unsafe_artifact_type(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="unsafe"):
        store.put_bytes(
            task_id=uuid4(),
            artifact_type="../../escape",
            content=b"nope",
            created_by="test",
        )
