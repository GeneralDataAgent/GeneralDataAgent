"""Offline, template-only candidates from narrowly evidenced tool repairs.

Input traces are development evidence, not authenticated production records.
Tool recovery does not establish answer correctness or causal reuse benefits.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import Field

from generaldataagent.business.actions import content_hash
from generaldataagent.business.experience import ExperienceRecord
from generaldataagent.contracts.models import ContractModel


class ExtractedExperience(ContractModel):
    """Review provenance kept outside the runtime-searchable lesson."""

    template_version: str = "rule-excerpt-v1"
    failure_step: int = Field(ge=1)
    recovery_step: int = Field(ge=1)
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    record: ExperienceRecord


def rule_repair_context_hash(arguments: dict) -> str:
    """Bind all call parameters except the specific field being repaired."""
    return content_hash(
        {key: value for key, value in arguments.items() if key != "semantics_excerpt"}
    )


def extract_rule_experience(
    trace: list[dict], *, documents: str, owner: str, created_at: datetime
) -> tuple[ExtractedExperience, ...]:
    """Extract at most one excerpt-repair candidate per trace; never activate it.

    Only adjacent error/success events qualify. Older traces without a bound
    failure context, intervening failures and changed parameters are skipped.
    No arbitrary trace text is copied into the fixed lesson template.
    """
    for failure, success in zip(trace, trace[1:], strict=False):
        if not isinstance(failure, dict) or not isinstance(success, dict):
            continue
        if (
            failure.get("status") != "error"
            or failure.get("tool") != "rules"
            or failure.get("code") != "rule_semantics_excerpt_missing"
            or success.get("status") != "ok"
            or success.get("tool") != "fee_rules"
        ):
            continue
        start, end = failure.get("step"), success.get("step")
        if type(start) is not int or type(end) is not int or start < 1 or end != start + 1:
            continue
        action = success.get("action")
        evidence = success.get("rule_evidence")
        if not isinstance(action, dict) or set(action) != {"rules"}:
            continue
        arguments = action["rules"]
        if not isinstance(arguments, dict) or not isinstance(evidence, dict):
            continue
        if failure.get("rule_repair_context_hash") != rule_repair_context_hash(arguments):
            continue
        excerpt = arguments.get("semantics_excerpt")
        if (
            not isinstance(excerpt, str)
            or not excerpt
            or excerpt not in documents
            or "null" not in excerpt.casefold()
            or evidence.get("semantics_excerpt") != excerpt
            or evidence.get("method_id") != "fee_rules"
            or evidence.get("complete") is not True
        ):
            continue
        checks = evidence.get("checks")
        if not isinstance(checks, list) or not {
            "source_hash",
            "typed_annex",
            "exact_rule_records",
        } <= set(check for check in checks if isinstance(check, str)):
            continue
        digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, default=str).encode()
        ).hexdigest()
        if success.get("result_hash") != digest:
            continue
        record = ExperienceRecord(
            failure_class="rule_semantics_excerpt_missing",
            applies_when=("a rule call rejects an excerpt absent from the supplied documents",),
            does_not_apply_when=(
                "the documents do not establish the required null interpretation",
                "other call parameters or wildcard semantics must change",
            ),
            repair=(
                "Locate a verbatim passage documenting null semantics in the supplied documents; "
                "replace only semantics_excerpt and retry within the existing repair budget. "
                "Observed tool acceptance still requires independent review of the interpretation."
            ),
            counterexample=(
                "A fabricated or paraphrased passage remains invalid; missing documentation "
                "does not justify inventing semantics or relaxing wildcard defaults."
            ),
            tool_scope=("rules",),
            owner=owner,
            created_at=created_at,
        )
        return (
            ExtractedExperience(
                failure_step=start, recovery_step=end, result_hash=digest, record=record
            ),
        )
    return ()
