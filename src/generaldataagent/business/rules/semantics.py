"""One explicit, evidence-backed reading of an annex's wildcard semantics.

A rule annex is only meaningful together with a declaration of what an
unconstrained field means. Two readings are possible and they are not
interchangeable:

* ``null`` means "any value" — stated in the DABstep manual;
* an empty list means "any value" — **not** stated there, yet most rules use an
  empty list for exactly that purpose.

The engine defaults to the fail-closed reading (an empty list matches nothing),
which makes the rule path return zero results on that annex. Rather than quietly
flipping the default, a caller must now *declare* the reading it relies on, quote
the passage it took it from, and carry that declaration's fingerprint into the
run. See ``docs/validation/stage10-rule-semantics.md`` for the evidence.

This module never infers a reading from the data and never reads a label.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from generaldataagent.contracts.models import ContractModel
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest


class SemanticsDeclarationError(ValueError):
    """Stable refusal code; never echoes the document text."""

    CODES = frozenset(
        {
            "semantics_declaration_source_missing",
            "semantics_declaration_null_source_missing",
            "semantics_declaration_request_conflict",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unknown semantics declaration failure code")
        self.code = code
        super().__init__(code)


class AnnexSemanticsDeclaration(ContractModel):
    """A caller's reading of one annex, with the passage that supports it.

    ``source_excerpt`` must be copied verbatim from the supplied documents; the
    declaration is refused if it is not. ``empty_list_is_wildcard`` has no
    default on purpose: a reading that is not chosen is not a reading.
    """

    null_is_wildcard: bool = True
    empty_list_is_wildcard: bool
    unknown_observation: Literal["skip", "reject"] = "reject"
    source_excerpt: str = Field(min_length=1, max_length=2_000)

    def validated_against(self, documents: str) -> AnnexSemanticsDeclaration:
        """Refuse a declaration the supplied documents do not support."""
        if not isinstance(documents, str):
            raise ValueError("documents must be text")
        if self.source_excerpt not in documents:
            raise SemanticsDeclarationError("semantics_declaration_source_missing")
        if self.null_is_wildcard and "null" not in self.source_excerpt.casefold():
            raise SemanticsDeclarationError("semantics_declaration_null_source_missing")
        return self

    def apply_to(self, request: RuleAnalysisRequest) -> RuleAnalysisRequest:
        """Return the request with this declaration's reading, or refuse a conflict.

        A conflict is never resolved in the declaration's favour: the caller
        built a request that says something else, and quietly overriding it
        would make the two records disagree.
        """
        if request.empty_list_is_wildcard != self.empty_list_is_wildcard:
            raise SemanticsDeclarationError("semantics_declaration_request_conflict")
        return request

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def summary(self) -> dict[str, object]:
        """What a run report records, without the quoted document text."""
        return {
            "null_is_wildcard": self.null_is_wildcard,
            "empty_list_is_wildcard": self.empty_list_is_wildcard,
            "unknown_observation": self.unknown_observation,
            "fingerprint": self.fingerprint(),
        }


__all__ = [
    "AnnexSemanticsDeclaration",
    "SemanticsDeclarationError",
]
