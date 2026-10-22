"""Document-derived rule contracts and deterministic fee execution."""

from generaldataagent.business.rules.engine import (
    RuleSourceError,
    applicable_rules,
    average_fee,
    compute_fee,
    counterfactual_delta,
    fee_results,
    load_rule_set,
    match_rules,
    monthly_totals,
    parse_band,
    parse_capture_delay,
    quantize,
    transaction_matches,
    volume_ratio,
)

__all__ = [
    "RuleSourceError",
    "applicable_rules",
    "average_fee",
    "compute_fee",
    "counterfactual_delta",
    "fee_results",
    "load_rule_set",
    "match_rules",
    "monthly_totals",
    "parse_band",
    "parse_capture_delay",
    "quantize",
    "transaction_matches",
    "volume_ratio",
]
