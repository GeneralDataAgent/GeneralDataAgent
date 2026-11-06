"""Deterministic binding of the three business report shapes to a TaskRubric.

The registry contains only repository-owned v1 templates. Binding adds every
template requirement to the rubric and never removes or weakens one. Metric
alias resolution remains upstream: the caller must pass an already-resolved
metric name/version, exact source hashes and method identity. No model, API,
repair or automatic revision participates.
"""

from typing import get_args
from uuid import UUID

from generaldataagent.contracts.rubric import (
    RUBRIC_VERIFIER_IDS,
    RubricDeliveryRequirement,
    RubricMethodRequirement,
    RubricSourceRequirement,
    TaskRubric,
    rubric_payload,
)
from generaldataagent.contracts.rubric_template import (
    BusinessRubricTemplate,
    BusinessTemplateId,
    RubricClaimType,
    template_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash

__all__ = [
    "RubricTemplateRefused",
    "bind_business_template",
    "business_rubric_template",
    "registered_templates",
]

_CLAIM_TYPES = frozenset(get_args(RubricClaimType))


class RubricTemplateRefused(ValueError):
    """The template or binding cannot produce a complete deterministic rubric."""


def _template(
    template_id: BusinessTemplateId,
    *,
    min_sources: int,
    required_claim_types: tuple[RubricClaimType, ...],
    human_confirmation_items: tuple[str, ...],
) -> BusinessRubricTemplate:
    verifier_ids = tuple(sorted(RUBRIC_VERIFIER_IDS))
    payload = template_payload(
        template_id=template_id,
        template_version="v1",
        min_sources=min_sources,
        required_claim_types=required_claim_types,
        human_confirmation_items=human_confirmation_items,
        verifier_ids=verifier_ids,
    )
    return BusinessRubricTemplate(
        template_id=template_id,
        template_version="v1",
        min_sources=min_sources,
        required_claim_types=required_claim_types,
        human_confirmation_items=human_confirmation_items,
        verifier_ids=verifier_ids,
        template_digest=canonical_hash(payload),
    )


_TEMPLATES: dict[str, BusinessRubricTemplate] = {
    "business.weekly_report": _template(
        "business.weekly_report",
        min_sources=1,
        required_claim_types=("fact", "interpretation", "limitation"),
        human_confirmation_items=("business_owner_signoff",),
    ),
    "business.channel_change": _template(
        "business.channel_change",
        min_sources=2,
        required_claim_types=("fact", "interpretation", "limitation"),
        human_confirmation_items=("channel_owner_signoff",),
    ),
    "business.anomaly_list": _template(
        "business.anomaly_list",
        min_sources=1,
        required_claim_types=("fact", "recommendation", "limitation"),
        human_confirmation_items=("operations_owner_signoff",),
    ),
}


def registered_templates() -> tuple[str, ...]:
    return tuple(sorted(_TEMPLATES))


def _verify_template(template: BusinessRubricTemplate) -> None:
    payload = template_payload(
        template_id=template.template_id,
        template_version=template.template_version,
        min_sources=template.min_sources,
        required_claim_types=template.required_claim_types,
        human_confirmation_items=template.human_confirmation_items,
        verifier_ids=template.verifier_ids,
    )
    if template.template_digest != canonical_hash(payload):
        raise RubricTemplateRefused("template_digest_mismatch")
    if set(template.verifier_ids) != RUBRIC_VERIFIER_IDS:
        raise RubricTemplateRefused("template_incomplete_verifier_set")


def business_rubric_template(template_id: str) -> BusinessRubricTemplate:
    template = _TEMPLATES.get(template_id)
    if template is None:
        raise RubricTemplateRefused("template_unknown")
    _verify_template(template)
    return template


def _require_source_requirements(
    sources: tuple[RubricSourceRequirement, ...],
    *,
    minimum: int,
) -> None:
    if not isinstance(sources, tuple) or any(
        not isinstance(item, RubricSourceRequirement) for item in sources
    ):
        raise RubricTemplateRefused("template_source_requirement_invalid")
    if len(sources) < minimum:
        raise RubricTemplateRefused("template_sources_missing")
    if len({item.source_id for item in sources}) != len(sources):
        raise RubricTemplateRefused("template_duplicate_source")
    if any(item.verifier_id != "rubric.sources_single_snapshot" for item in sources):
        raise RubricTemplateRefused("template_source_verifier_mismatch")


def _require_method(method: RubricMethodRequirement) -> None:
    if not isinstance(method, RubricMethodRequirement):
        raise RubricTemplateRefused("template_method_invalid")
    if method.verifier_id != "rubric.metric_version_bound":
        raise RubricTemplateRefused("template_method_verifier_mismatch")


def _require_extras(
    *,
    template: BusinessRubricTemplate,
    extra_claim_types: tuple[str, ...],
    extra_human_confirmation_items: tuple[str, ...],
) -> None:
    unknown = set(extra_claim_types) - _CLAIM_TYPES
    if unknown:
        raise RubricTemplateRefused("template_unknown_claim_type")
    if len(set(extra_claim_types)) != len(extra_claim_types):
        raise RubricTemplateRefused("template_duplicate_claim_type")
    if set(extra_claim_types) & set(template.required_claim_types):
        raise RubricTemplateRefused("template_duplicate_claim_type")
    if len(set(extra_human_confirmation_items)) != len(extra_human_confirmation_items):
        raise RubricTemplateRefused("template_duplicate_human_item")
    if set(extra_human_confirmation_items) & set(template.human_confirmation_items):
        raise RubricTemplateRefused("template_duplicate_human_item")


def bind_business_template(
    *,
    template_id: str,
    task_id: UUID,
    metric_name: str,
    metric_version: str,
    start_time: str,
    end_time: str,
    sources: tuple[RubricSourceRequirement, ...],
    method: RubricMethodRequirement,
    extra_claim_types: tuple[str, ...] = (),
    extra_human_confirmation_items: tuple[str, ...] = (),
) -> TaskRubric:
    """Build one complete rubric from a fixed template and exact task facts."""

    template = business_rubric_template(template_id)
    _require_source_requirements(sources, minimum=template.min_sources)
    _require_method(method)
    _require_extras(
        template=template,
        extra_claim_types=extra_claim_types,
        extra_human_confirmation_items=extra_human_confirmation_items,
    )
    claim_types = tuple(dict.fromkeys((*template.required_claim_types, *extra_claim_types)))
    human_items = tuple(
        dict.fromkeys((*template.human_confirmation_items, *extra_human_confirmation_items))
    )
    delivery = RubricDeliveryRequirement(
        required_claim_types=claim_types,
        human_confirmation_items=human_items,
        verifier_ids=template.verifier_ids,
    )
    rubric_id = f"{template.template_id}.{task_id}.{metric_version}"
    payload = rubric_payload(
        rubric_id=rubric_id,
        task_id=task_id,
        metric_name=metric_name,
        metric_version=metric_version,
        start_time=start_time,
        end_time=end_time,
        sources=sources,
        method=method,
        delivery=delivery,
    )
    return TaskRubric(
        rubric_id=rubric_id,
        task_id=task_id,
        metric_name=metric_name,
        metric_version=metric_version,
        start_time=start_time,
        end_time=end_time,
        sources=sources,
        method=method,
        delivery=delivery,
        rubric_digest=canonical_hash(payload),
    )
