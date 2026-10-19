"""Explicit synthetic reviews for local admission tests; never external acceptance evidence."""

from datetime import UTC, datetime, timedelta

from generaldataagent.business.governance import Capability

HASH = "sha256:" + "a" * 64


def enroll(registry, actor, kind, name, digest, version="v1"):
    record = Capability(
        name=name,
        version=version,
        kind=kind,
        content_hash=digest,
        owner="fixture-owner",
        tenant_id=actor.tenant_id,
        project_id=actor.project_id,
        data_domains=actor.data_domains,
        regression_evidence_hash=HASH,
        security_evidence_hash=HASH,
        rollback_version="disabled",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    registry.propose(record)
    registry.review(name, version, reviewer_id="fixture-quality", discipline="quality")
    registry.review(name, version, reviewer_id="fixture-security", discipline="security")
    registry.activate(name, version)
