"""Use the shared rule application service locally without evaluation or a model.

This is the non-benchmark caller that proves the shared operations are reusable:
it imports only `business.rules` and the contracts, never `evaluation`, a model
provider, or a LangGraph runtime.
"""

from decimal import Decimal

from generaldataagent.business.rules.application import (
    compute_fee_summary,
    resolve_applicability,
    resolve_target_month,
    rule_affected_amounts,
    scope_rule_month,
    select_rule_facets,
)
from generaldataagent.business.rules.engine import load_rule_set
from generaldataagent.business.rules.payments import transaction_facets
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.contracts.rules import RuleSemantics, TransactionFacets

ANNEX = [
    dict(ID="demo-retail", card_scheme="Orbit", account_type=["R"], fixed_amount="0.07", rate="13"),
    dict(
        ID="demo-digital",
        card_scheme="Orbit",
        account_type=["D"],
        monthly_volume=">1000",
        fixed_amount="0.03",
        rate="17",
    ),
    dict(
        ID="demo-foreign",
        card_scheme="Orbit",
        fixed_amount="0.05",
        rate="11",
        intracountry=False,
    ),
]

PAYMENTS = [
    dict(
        year=2026,
        day_of_year=1,
        eur_amount="1200.00",
        has_fraudulent_dispute="False",
        card_scheme="Orbit",
        aci="B",
        is_credit="True",
        issuing_country="FR",
        acquirer_country="DE",
    ),
    dict(
        year=2026,
        day_of_year=2,
        eur_amount="800.00",
        has_fraudulent_dispute="True",
        card_scheme="Orbit",
        aci="B",
        is_credit="False",
        issuing_country="FR",
        acquirer_country="FR",
    ),
    dict(
        year=2026,
        day_of_year=40,
        eur_amount="50.00",
        has_fraudulent_dispute="False",
        card_scheme="Nova",
        aci="F",
        is_credit="False",
        issuing_country="FR",
        acquirer_country="FR",
    ),
]

MERCHANT = {"merchant": "Demo Shop", "account_type": "R", "merchant_category_code": 5999}


def main():
    rules = load_rule_set(
        ANNEX, source="synthetic-annex-v1", semantics=RuleSemantics(source="synthetic-manual-v1")
    )
    selection = RuleAnalysisRequest(
        operation="select", account_type="R", semantics_excerpt="null accepts all values"
    )
    print("selected:", [rule.rule_id for rule in select_rule_facets(rules, selection)])

    # A day-scoped question still uses the whole natural month for thresholds.
    month = resolve_target_month(year=2026, month=None, day_of_year=2)
    scope = scope_rule_month(PAYMENTS, month=month, day_of_year=2)
    print("month:", scope.month, "volume:", scope.volume, "fraud_ratio:", scope.fraud_ratio)
    print("scoped_transactions:", len(scope.scoped_rows))

    transactions = tuple(TransactionFacets(**facet) for facet in transaction_facets(PAYMENTS[:2]))
    applicability = resolve_applicability(
        rules,
        merchant=MERCHANT,
        volume=scope.volume,
        fraud_ratio=scope.fraud_ratio,
        transactions=transactions,
    )
    print("matched:", [rule.rule_id for rule in applicability.matched])
    print("indeterminate:", applicability.indeterminate)

    fees = compute_fee_summary(applicability.matched, transaction_value=Decimal("100"))
    print("average_fee:", fees.average)
    target = next(rule for rule in applicability.matched if rule.rule_id == "demo-foreign")
    print("affected_by_demo_foreign:", rule_affected_amounts(target, transactions))


if __name__ == "__main__":
    main()
