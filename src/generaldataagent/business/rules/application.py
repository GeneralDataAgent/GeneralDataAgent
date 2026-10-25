"""Shared rule application operations, independent of workflow and benchmark transport."""

from collections.abc import Sequence

from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.contracts.rules import FeeRule


def select_rule_facets(
    rules: Sequence[FeeRule], request: RuleAnalysisRequest
) -> tuple[FeeRule, ...]:
    """Filter only explicitly supplied facets, retaining source order and identity.

    Merchant/month thresholds and transaction applicability are later operations.
    Wildcard behavior is already bound into the typed rules by their loader.
    """
    return tuple(
        rule
        for rule in rules
        if (
            (request.card_scheme is None or rule.card_scheme == request.card_scheme)
            and (request.account_type is None or rule.account_type.matches(request.account_type))
            and (
                request.merchant_category_code is None
                or rule.merchant_category_code.matches(request.merchant_category_code)
            )
            and (request.aci is None or rule.aci.matches(request.aci))
            and (request.is_credit is None or rule.is_credit in (None, request.is_credit))
            and (request.intracountry is None or rule.intracountry in (None, request.intracountry))
        )
    )
