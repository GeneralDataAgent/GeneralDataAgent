"""Receipt projections of authoritative rule checkpoint metadata."""

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from generaldataagent.contracts import ReceiptVerification, SourceBinding


def receipt_sources(source_hashes: Mapping[str, str]) -> tuple[SourceBinding, ...]:
    # Alias all names together to avoid collisions with a literal generated name.
    preserve = all(re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name) for name in source_hashes)
    names = sorted(source_hashes)
    identifiers = [
        name if preserve else "s" + hashlib.sha256(name.encode()).hexdigest()[:63] for name in names
    ]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("rule_receipt_source_identifier_collision")
    return tuple(
        SourceBinding(source_id=identifier, content_hash=source_hashes[name])
        for name, identifier in zip(names, identifiers, strict=True)
    )


def receipt_verification(
    verification: Mapping[str, Any] | None, *, scope: str = "producer_self_check"
) -> ReceiptVerification:
    """Use the wrapper-owned scope; handler-returned scope cannot strengthen it."""
    if verification is None:
        return ReceiptVerification(scope="none", passed=False)
    checks = verification.get("checks", [])
    if not isinstance(checks, list) or any(
        not isinstance(code, str) or not code for code in checks
    ):
        raise ValueError("rule_receipt_checks_invalid")
    if scope not in {"producer_self_check", "authoritative_rederivation"}:
        raise ValueError("rule_receipt_verification_scope_invalid")
    if not checks:
        return ReceiptVerification(scope="none", passed=False)
    return ReceiptVerification(
        scope=scope, passed=verification["passed"], check_codes=tuple(checks)
    )
