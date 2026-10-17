import json
from uuid import uuid4

import pytest

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext
from generaldataagent.security.dlp import DLPDeniedError, TextDLPScanner


@pytest.mark.parametrize(
    "content,reason",
    [
        (b'{"api\\u005fkey":"abcdefgh123456"}', "credential"),
        (b'{"contact":"alice\\u0040example.com"}', "email"),
        (
            b'{"phone":"\\u0031\\u0033\\u0038\\u0030\\u0030\\u0031\\u0033\\u0038\\u0030\\u0030\\u0030"}',
            "phone",
        ),
        ('{"ｐａｓｓｗｏｒｄ":"abcdefgh123456"}'.encode(), "credential"),
        (b'{"pass\\u200bword":"abcdefgh123456"}', "credential"),
        (json.dumps({"nested": [{"secret": "abcdefgh123456"}]}).encode(), "credential"),
    ],
)
def test_semantic_json_and_unicode_sensitive_content_is_rejected(content, reason):
    with pytest.raises(DLPDeniedError, match=reason) as error:
        TextDLPScanner().check(content)
    assert "abcdefgh123456" not in str(error.value)
    assert "alice" not in str(error.value)


def test_safe_encoded_reports_are_unchanged_and_failures_never_write_artifacts(tmp_path):
    scanner = TextDLPScanner()
    safe = b'{"label":"\\u6536\\u5165", "hash":"a13800138000b", "value":20}'
    scanner.check(safe)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    store = GovernedArtifactStore(tmp_path / "content", index_path=tmp_path / "index", actor=actor)
    args = dict(task_id=uuid4(), artifact_type="report", created_by="reporter")
    ref = store.put_bytes(**args, content=safe)
    assert store.read_bytes(ref) == safe
    before = tuple((tmp_path / "content").rglob("*"))
    with pytest.raises(DLPDeniedError):
        store.put_bytes(**args, content=b'{"api\\u005fkey":"abcdefgh123456"}')
    assert tuple((tmp_path / "content").rglob("*")) == before
    with pytest.raises(DLPDeniedError, match="structure"):
        scanner.check(b'{"value":NaN}')
    with pytest.raises(DLPDeniedError, match="structure"):
        scanner.check(b"[" * 5000 + b"0" + b"]" * 5000)
