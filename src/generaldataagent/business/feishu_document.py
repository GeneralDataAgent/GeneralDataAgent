"""Publish a report as a Feishu cloud document or file, with the failure semantics first.

No real application is registered here and no CLI syntax is invented: this module
defines the transport port, the deterministic naming that makes a retry find what
a previous attempt already wrote, and the three outcomes a caller must be able to
tell apart:

* it was published now;
* it already existed (the idempotency key matched an earlier attempt), so nothing
  was written twice;
* the outcome is unknown, and retrying may duplicate it.

The third case is the one that matters. A business loop that treats "the call
threw" as "nothing happened" publishes twice; one that treats it as "it landed"
records a document that does not exist. So an ambiguous attempt raises instead of
being retried, and the caller records the business action as ``uncertain`` until
someone reads the folder back.
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol

MAX_PUBLICATION_BYTES = 10 * 1024 * 1024


class PublicationRefused(PermissionError):
    """The transport refused before writing anything; a retry is safe."""


class PublicationAmbiguous(PermissionError):
    """The attempt may or may not have landed; retrying may duplicate it."""


class PublicationMismatch(PermissionError):
    """What the service returns does not describe what was sent."""


class FeishuDocumentTransport(Protocol):
    """Cloud-doc / drive operations, scoped to one authenticated identity."""

    def identity(self) -> dict: ...
    def find(self, *, name: str, folder_token: str | None) -> list[dict]: ...
    def publish(self, *, name: str, content: bytes, folder_token: str | None, key: str) -> dict: ...
    def read_back(self, *, token: str, identity: str) -> dict | None: ...


@dataclass(frozen=True, slots=True)
class Publication:
    name: str
    token: str
    url: str | None
    reused: bool
    size_bytes: int | None
    """The size the service reported, or ``None`` when it reported none."""


def publication_name(report_name: str, idempotency_key: str) -> str:
    """A stable, filesystem- and URL-safe name that carries the retry key.

    The key is part of the name so a retry — in this process or after a restart —
    can find the earlier attempt instead of writing a second file under a name
    nobody will look for.
    """
    stem = "".join(
        character if character.isalnum() or character in "-_." else "-" for character in report_name
    ).strip("-._")
    if not stem or len(stem) > 120:
        raise ValueError("publication name must be 1-120 safe characters")
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]
    return f"{stem}-{digest}"


def publish_document(
    transport: FeishuDocumentTransport,
    *,
    report_name: str,
    content: bytes,
    folder_token: str | None,
    idempotency_key: str,
    max_bytes: int = MAX_PUBLICATION_BYTES,
) -> Publication:
    """Publish once, or report precisely why that could not be established."""
    if not content:
        raise ValueError("refusing to publish an empty document")
    if len(content) > max_bytes:
        raise ValueError("document exceeds the publication size cap")
    if not 1 <= len(idempotency_key) <= 128:
        raise ValueError("idempotency key must be 1-128 characters")
    name = publication_name(report_name, idempotency_key)

    # Look before writing: a previous attempt may have landed and only its reply
    # was lost. Finding it is a successful publication, not a conflict.
    try:
        existing = transport.find(name=name, folder_token=folder_token)
    except PublicationRefused:
        raise
    except Exception as exc:
        raise PublicationAmbiguous("could not establish whether the document exists") from exc
    for record in existing:
        if record.get("name") == name and record.get("token"):
            # Finding the name is not finding the bytes. A document edited,
            # truncated or replaced after the earlier attempt would otherwise be
            # reported as a fresh publication of what this call is holding.
            current = _read_back(
                transport,
                token=record["token"],
                content=content,
                when_unreadable="the service lists an existing document it cannot read back",
            )
            return Publication(
                name=name,
                token=record["token"],
                url=current.get("url") or record.get("url"),
                reused=True,
                size_bytes=current.get("size_bytes"),
            )

    try:
        published = transport.publish(
            name=name, content=content, folder_token=folder_token, key=idempotency_key
        )
    except PublicationRefused:
        raise
    except Exception as exc:
        raise PublicationAmbiguous("the publish attempt may have landed") from exc
    token = published.get("token")
    if not isinstance(token, str) or not token:
        raise PublicationAmbiguous("the service accepted the call but returned no document token")

    # A token is not a document. Read it back before reporting success.
    record = _read_back(
        transport,
        token=token,
        content=content,
        when_unreadable="the service does not list the document it just returned",
    )
    return Publication(
        name=name,
        token=token,
        url=record.get("url") or published.get("url"),
        reused=False,
        size_bytes=record.get("size_bytes"),
    )


def _read_back(
    transport: FeishuDocumentTransport,
    *,
    token: str,
    content: bytes,
    when_unreadable: str,
) -> dict:
    """Read the document behind a token and refuse if it is not what was sent.

    Both the fresh write and the retry route through here: a token from the
    service, or a name that matched, is not a document. Where the service reports
    a size or a hash, they must describe the bytes this call is holding; where it
    reports neither, the size stays unknown rather than being filled in from what
    was intended.
    """
    try:
        identity = str(transport.identity().get("identity", ""))
        record = transport.read_back(token=token, identity=identity)
    except Exception as exc:
        raise PublicationAmbiguous(when_unreadable) from exc
    if record is None:
        raise PublicationAmbiguous(when_unreadable)
    size = record.get("size_bytes")
    if size is not None and size != len(content):
        raise PublicationMismatch("the stored document size differs from what was sent")
    digest = record.get("content_hash")
    if digest is not None and digest != f"sha256:{hashlib.sha256(content).hexdigest()}":
        raise PublicationMismatch("the stored content hash does not match what was sent")
    return record
