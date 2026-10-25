"""Use shared rule selection locally without evaluation, model, or graph dependencies."""

from generaldataagent.business.rules.application import select_rule_facets
from generaldataagent.business.rules.engine import load_rule_set
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.contracts.rules import RuleSemantics


def main():
    rules = load_rule_set(
        [
            dict(
                ID="demo-retail",
                card_scheme="Orbit",
                account_type=["R"],
                fixed_amount="0.07",
                rate="13",
            ),
            dict(
                ID="demo-digital",
                card_scheme="Orbit",
                account_type=["D"],
                fixed_amount="0.03",
                rate="17",
            ),
        ],
        source="synthetic-annex-v1",
        semantics=RuleSemantics(source="synthetic-manual-v1"),
    )
    request = RuleAnalysisRequest(
        operation="select", account_type="R", semantics_excerpt="null accepts all values"
    )
    selected = select_rule_facets(rules, request)
    print([rule.rule_id for rule in selected])


if __name__ == "__main__":
    main()
