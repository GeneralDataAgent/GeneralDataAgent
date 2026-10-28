"""A cloud-doc publication must never be retried without knowing what happened."""

import hashlib

import pytest

from generaldataagent.business.feishu_document import (
    PublicationAmbiguous,
    PublicationMismatch,
    PublicationRefused,
    publication_name,
    publish_document,
)


class FakeTransport:
    def __init__(self, *, existing=(), fail_find=None, fail_publish=None, read_back=None):
        self.files = {record["name"]: dict(record) for record in existing}
        self.fail_find = fail_find
        self.fail_publish = fail_publish
        self.read_back_mode = read_back
        self.published = []
        self.identities = 0

    def identity(self):
        self.identities += 1
        return {"identity": "bot"}

    def find(self, *, name, folder_token):
        if self.fail_find is not None:
            raise self.fail_find
        return [dict(self.files[name])] if name in self.files else []

    def publish(self, *, name, content, folder_token, key):
        if self.fail_publish is not None:
            raise self.fail_publish
        token = f"tok-{len(self.published) + 1}"
        self.files[name] = {"name": name, "token": token, "url": f"https://example/{token}"}
        self.published.append((name, content, key))
        return {"token": token, "url": f"https://example/{token}"}

    def read_back(self, *, token, identity):
        if self.read_back_mode == "missing":
            return None
        for record in self.files.values():
            if record["token"] == token:
                return {
                    "size_bytes": len(self.published[-1][1]) if self.published else 0,
                    **record,
                }
        return None


def publish(transport, content=b"report body", name="weekly-report"):
    return publish_document(
        transport,
        report_name=name,
        content=content,
        folder_token="fld-1",
        idempotency_key="run-2026-10-29",
    )


def test_a_first_publication_is_written_once_and_read_back():
    transport = FakeTransport()
    result = publish(transport)
    assert result.reused is False
    assert result.size_bytes == len(b"report body")
    assert [entry[0] for entry in transport.published] == [result.name]
    assert transport.identities == 1


def test_a_retry_with_the_same_key_finds_the_earlier_document_and_writes_nothing():
    transport = FakeTransport()
    first = publish(transport)
    second = publish(transport)
    assert second.reused is True
    assert second.token == first.token
    assert len(transport.published) == 1


def test_a_key_that_already_landed_is_found_even_after_a_restart():
    name = publication_name("weekly-report", "run-2026-10-29")
    transport = FakeTransport(existing=[{"name": name, "token": "tok-old"}])
    # No in-memory state survives: the folder is the only thing carried over.
    result = publish(transport)
    assert result.reused is True and result.token == "tok-old"
    assert transport.published == []


def test_an_attempt_that_may_have_landed_is_never_retried_as_a_failure():
    transport = FakeTransport(fail_publish=TimeoutError("no reply"))
    with pytest.raises(PublicationAmbiguous, match="may have landed"):
        publish(transport)
    assert transport.published == []


def test_a_failed_lookup_is_ambiguous_not_absent():
    transport = FakeTransport(fail_find=TimeoutError("no reply"))
    with pytest.raises(PublicationAmbiguous, match="whether the document exists"):
        publish(transport)
    assert transport.published == []


def test_a_refusal_before_writing_is_safe_to_retry_and_says_so():
    transport = FakeTransport(fail_publish=PublicationRefused("feishu_identity_unavailable"))
    with pytest.raises(PublicationRefused):
        publish(transport)


def test_a_token_without_a_readable_document_is_not_success():
    transport = FakeTransport(read_back="missing")
    with pytest.raises(PublicationAmbiguous, match="does not list the document"):
        publish(transport)


def test_a_read_back_with_another_size_is_a_mismatch():
    transport = FakeTransport()
    transport.read_back = lambda **_: {"token": "tok-1", "size_bytes": 999}
    with pytest.raises(PublicationMismatch, match="size"):
        publish(transport)


def test_the_name_carries_the_key_and_stays_safe():
    first = publication_name("Q3 report: sales/revenue", "run-1")
    assert first == publication_name("Q3 report: sales/revenue", "run-1")
    assert first != publication_name("Q3 report: sales/revenue", "run-2")
    assert ":" not in first and "/" not in first and " " not in first
    with pytest.raises(ValueError, match="safe characters"):
        publication_name("///", "run-1")


def test_an_empty_or_oversized_document_never_reaches_the_transport():
    transport = FakeTransport()
    with pytest.raises(ValueError, match="empty document"):
        publish(transport, content=b"")
    with pytest.raises(ValueError, match="size cap"):
        publish_document(
            transport,
            report_name="weekly-report",
            content=b"x" * 11,
            folder_token=None,
            idempotency_key="k",
            max_bytes=10,
        )
    assert transport.published == []
    assert transport.identities == 0


def test_a_read_back_hash_must_match_the_sent_bytes():
    transport = FakeTransport()
    transport.read_back = lambda **_: {
        "token": "tok-1",
        "content_hash": "sha256:" + "0" * 64,
    }
    with pytest.raises(PublicationMismatch, match="hash"):
        publish(transport)
    transport.read_back = lambda **_: {
        "token": "tok-1",
        "content_hash": "sha256:" + hashlib.sha256(b"report body").hexdigest(),
    }
    fresh = FakeTransport()
    fresh.read_back = lambda **_: {
        "token": "tok-1",
        "content_hash": "sha256:" + hashlib.sha256(b"report body").hexdigest(),
    }
    assert publish(fresh).reused is False
