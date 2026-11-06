"""Fixed business rubric templates for the three Stage 14 report shapes.

A template is not a prompt and does not resolve metrics. It binds one named
business report shape to the complete deterministic verifier set, required
claim types, human-confirmation items and a minimum source count. Binding is
deterministic: template requirements are added to the rubric, never weakened,
and the caller still supplies the already-resolved metric, exact sources and
method. Unknown templates, claim types, duplicates or a digest mismatch are
refused before a rubric exists.
"""

from typing import Literal

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel
from generaldataagent.contracts.rubric import RUBRIC_VERIFIER_IDS
from generaldataagent.memory.display_preferences import canonical_hash

TEMPLATE_VERSION = "1.0"

BusinessTemplateId = Literal[
    "business.weekly_report",
    "business.channel_change",
    "business.anomaly_list",
]
RubricClaimType = Literal["fact", "interpretation", "limitation", "recommendation"]


class BusinessRubricTemplate(ContractModel):
    """One immutable template, bound to a complete deterministic verifier set."""

    schema_version: Literal["1.0"] = "1.0"
    template_id: BusinessTemplateId
    template_version: Literal["v1"] = "v1"
    min_sources: int = Field(ge=1, le=8)
    required_claim_types: tuple[RubricClaimType, ...] = Field(min_length=1, max_length=4)
    human_confirmation_items: tuple[str, ...] = Field(default=(), max_length=8)
    verifier_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    template_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def complete_template(self):
        claims = self.required_claim_types
        if "fact" not in claims or "limitation" not in claims:
            raise ValueError("template_claim_types_incomplete")
        if len(set(claims)) != len(claims):
            raise ValueError("template_duplicate_claim_type")
        if len(set(self.human_confirmation_items)) != len(self.human_confirmation_items):
            raise ValueError("template_duplicate_human_item")
        if not set(self.verifier_ids) <= RUBRIC_VERIFIER_IDS:
            raise ValueError("template_unknown_verifier")
        if len(set(self.verifier_ids)) != len(self.verifier_ids):
            raise ValueError("template_duplicate_verifier")
        if set(self.verifier_ids) != RUBRIC_VERIFIER_IDS:
            raise ValueError("template_incomplete_verifier_set")
        payload = template_payload(
            template_id=self.template_id,
            template_version=self.template_version,
            min_sources=self.min_sources,
            required_claim_types=claims,
            human_confirmation_items=self.human_confirmation_items,
            verifier_ids=self.verifier_ids,
        )
        if self.template_digest != canonical_hash(payload):
            raise ValueError("template_digest_mismatch")
        return self


def template_payload(
    *,
    template_id: str,
    template_version: str,
    min_sources: int,
    required_claim_types: tuple[str, ...],
    human_confirmation_items: tuple[str, ...],
    verifier_ids: tuple[str, ...],
) -> dict:
    """Canonical bytes bound by ``BusinessRubricTemplate.template_digest``."""

    return {
        "schema_version": TEMPLATE_VERSION,
        "template_id": template_id,
        "template_version": template_version,
        "min_sources": min_sources,
        "required_claim_types": list(required_claim_types),
        "human_confirmation_items": list(human_confirmation_items),
        "verifier_ids": list(verifier_ids),
    }


__all__ = [
    "TEMPLATE_VERSION",
    "BusinessRubricTemplate",
    "BusinessTemplateId",
    "RubricClaimType",
    "template_payload",
]
