"""Deterministic checks for the document-derived fee rule engine."""

from decimal import Decimal

import pytest

from generaldataagent.business.rules import (
    RuleSourceError,
    applicable_rules,
    average_fee,
    counterfactual_delta,
    fee_results,
    load_rule_set,
    match_rules,
    monthly_totals,
    parse_band,
    parse_capture_delay,
    transaction_matches,
    volume_ratio,
)
from generaldataagent.contracts.rules import RuleSemantics, TransactionFacets

SOURCE = "merchant-fee-annex@2024-11-01"
NULL_WILD = RuleSemantics(null_is_wildcard=True, empty_list_is_wildcard=False, source=SOURCE)
EMPTY_WILD = RuleSemantics(null_is_wildcard=True, empty_list_is_wildcard=True, source=SOURCE)

ANNEX = [
    {
        "ID": "A",
        "card_scheme": "Alpha",
        "account_type": [],
        "capture_delay": None,
        "monthly_fraud_level": None,
        "monthly_volume": None,
        "merchant_category_code": [],
        "is_credit": True,
        "aci": ["B", "C"],
        "fixed_amount": 0.05,
        "rate": 14,
    },
    {
        "ID": "B",
        "card_scheme": "Alpha",
        "account_type": ["R"],
        "capture_delay": "<3",
        "monthly_fraud_level": "7.7%-8.3%",
        "monthly_volume": "100k-1m",
        "merchant_category_code": [5411],
        "is_credit": None,
        "aci": None,
        "fixed_amount": 0.1,
        "rate": 19,
    },
    {
        "ID": "C",
        "card_scheme": "Beta",
        "account_type": None,
        "capture_delay": "immediate",
        "monthly_fraud_level": ">8.3%",
        "monthly_volume": None,
        "merchant_category_code": [5411],
        "is_credit": False,
        "aci": ["A"],
        "intracountry": True,
        "fixed_amount": 0.13,
        "rate": 86,
    },
]


def test_band_parsing_is_exact_and_unit_aware():
    assert parse_band("7.7%-8.3%").contains(0.0782)
    assert not parse_band("7.7%-8.3%").contains(0.09)
    volume = parse_band("100k-1m")
    assert (volume.lower, volume.upper) == (100_000.0, 1_000_000.0)
    assert parse_band(">5m").contains(5_000_001)
    assert not parse_band(">5").contains(5)
    assert parse_band("<100k").contains(99_999.99)


def test_band_rejects_malformed_or_inverted_input():
    with pytest.raises(RuleSourceError):
        parse_band("between ten and twenty")
    with pytest.raises(RuleSourceError):
        parse_band("")
    with pytest.raises(ValueError):
        from generaldataagent.contracts.rules import NumericBand

        NumericBand(lower=5, upper=1, source_text="5-1")


def test_capture_delay_supports_bands_and_named_modes():
    band = parse_capture_delay("3-5")
    assert band.matches(4, None)
    assert not band.matches(2, None)
    mode = parse_capture_delay("manual")
    assert mode.matches(None, "manual")
    assert not mode.matches(3, None)
    assert parse_capture_delay(None) is None


def test_empty_list_is_never_treated_as_wildcard_without_declaration():
    strict = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    rule_a = next(rule for rule in strict if rule.rule_id == "A")
    assert not rule_a.account_type.matches("R")
    assert rule_a.aci.matches("B")

    wildcard = load_rule_set(ANNEX, source=SOURCE, semantics=EMPTY_WILD)
    wild_a = next(rule for rule in wildcard if rule.rule_id == "A")
    assert wild_a.account_type.matches("R")
    assert wild_a.merchant_category_code.matches(5411)


def test_null_fields_are_wildcards_but_empty_lists_are_not():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    rule_c = next(rule for rule in rules if rule.rule_id == "C")
    assert rule_c.account_type.matches("anything")
    assert rule_c.aci.matches("A")
    assert not rule_c.aci.matches("B")


def test_rule_source_is_recorded_on_every_rule():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    assert all(rule.source == SOURCE for rule in rules)


def test_duplicate_rule_ids_and_bad_amounts_fail_closed():
    with pytest.raises(RuleSourceError):
        load_rule_set([ANNEX[0], ANNEX[0]], source=SOURCE, semantics=NULL_WILD)
    bad = {**ANNEX[0], "fixed_amount": "1,50"}
    with pytest.raises(RuleSourceError):
        load_rule_set([bad], source=SOURCE, semantics=NULL_WILD)


def test_merchant_facets_combine_account_mcc_volume_fraud_and_delay():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    merchant = {
        "account_type": "R",
        "merchant_category_code": 5411,
        "capture_delay": "2",
    }
    matched = match_rules(
        rules, merchant=merchant, monthly_volume=116_436.32, monthly_fraud_level=0.0782
    )
    assert [rule.rule_id for rule in matched] == ["B"]

    too_small = match_rules(
        rules, merchant=merchant, monthly_volume=50_000.0, monthly_fraud_level=0.0782
    )
    assert too_small == ()

    fraud_too_high = match_rules(
        rules, merchant=merchant, monthly_volume=116_436.32, monthly_fraud_level=0.10
    )
    assert fraud_too_high == ()


def test_capture_delay_day_count_blocks_mismatched_rules():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    merchant = {"account_type": "R", "merchant_category_code": 5411, "capture_delay": "7"}
    assert (
        match_rules(rules, merchant=merchant, monthly_volume=116_436.32, monthly_fraud_level=0.0782)
        == ()
    )


def test_transaction_facets_filter_by_scheme_aci_credit_and_domesticity():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    rule_b = next(rule for rule in rules if rule.rule_id == "B")
    transaction = TransactionFacets(
        card_scheme="Alpha", aci="C", is_credit=True, intracountry=True, amount="10"
    )
    # Rule B leaves intracountry null, so domestic and foreign both qualify.
    assert transaction_matches(rule_b, transaction)
    assert transaction_matches(rule_b, transaction.model_copy(update={"intracountry": False}))
    # Rule B leaves is_credit null, so both credit and debit qualify.
    assert transaction_matches(rule_b, transaction.model_copy(update={"is_credit": False}))
    rule_c = next(rule for rule in rules if rule.rule_id == "C")
    assert not transaction_matches(rule_c, transaction.model_copy(update={"aci": "B"}))


def test_domestic_rule_requires_known_origin_instead_of_guessing():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    rule_c = next(rule for rule in rules if rule.rule_id == "C")
    unknown = TransactionFacets(
        card_scheme="Beta", aci="A", is_credit=False, intracountry=None, amount="10"
    )
    # An unknown origin cannot be proven to match, so the rule is not applied.
    assert transaction_matches(rule_c, unknown) is False
    assert transaction_matches(rule_c, unknown.model_copy(update={"intracountry": True})) is True
    assert transaction_matches(rule_c, unknown.model_copy(update={"intracountry": False})) is False


def test_applicable_rules_reports_matches_and_indeterminate_entries():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    transaction = TransactionFacets(
        card_scheme="Beta", aci="A", is_credit=False, intracountry=None, amount="10"
    )
    report = applicable_rules(rules, [transaction])
    assert report.matched == ()
    assert report.indeterminate == ("C",)
    assert report.unmatched_count == 2
    assert report.ambiguity is False
    assert "intracountry unknown" in report.notes[0]
    resolved = applicable_rules(rules, [transaction.model_copy(update={"intracountry": True})])
    assert resolved.matched == ("C",)
    assert resolved.unmatched_count == 2


def test_fee_is_fixed_plus_rate_times_value_over_ten_thousand():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    results = fee_results(rules, transaction_value=Decimal("10"), rounding="six_decimals")
    by_id = {result.rule_id: result for result in results}
    assert Decimal(by_id["A"].fee) == Decimal("0.064000")  # 0.05 + 14*10/10000
    assert Decimal(by_id["B"].fee) == Decimal("0.119000")
    assert Decimal(by_id["C"].fee) == Decimal("0.216000")
    assert by_id["A"].source == SOURCE


def test_average_fee_returns_none_without_rules_and_ignores_rounding_drift():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    assert average_fee((), transaction_value=Decimal("10")) is None
    mean = average_fee(rules, transaction_value=Decimal("10"))
    expected = (Decimal("0.064") + Decimal("0.119") + Decimal("0.216")) / 3
    assert Decimal(mean) == expected.quantize(Decimal("0.000001"))


def test_monthly_totals_separate_volume_ratio_from_count_ratio():
    rows = [
        {"eur_amount": "100.00", "has_fraudulent_dispute": "True"},
        {"eur_amount": "300.00", "has_fraudulent_dispute": "False"},
        {"eur_amount": "50.00", "has_fraudulent_dispute": "false"},
    ]
    volume, fraud_volume, count, fraud_count = monthly_totals(rows)
    assert (volume, fraud_volume, count, fraud_count) == (
        Decimal("450.00"),
        Decimal("100.00"),
        3,
        1,
    )
    assert volume_ratio(volume, fraud_volume) == pytest.approx(100 / 450)
    assert fraud_count / count == pytest.approx(1 / 3)


def test_monthly_totals_rejects_negative_and_unknown_booleans():
    with pytest.raises(ValueError):
        monthly_totals([{"eur_amount": "-1", "has_fraudulent_dispute": False}])
    with pytest.raises(ValueError):
        monthly_totals([{"eur_amount": "1", "has_fraudulent_dispute": "maybe"}])
    assert volume_ratio(Decimal(0), Decimal(0)) is None


def test_counterfactual_delta_uses_decimal_exact_arithmetic():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    rule_a = next(rule for rule in rules if rule.rule_id == "A")
    transactions = [Decimal("10"), Decimal("20.5")]
    result = counterfactual_delta(rule_a, transactions, alternate_rate=Decimal(1))
    baseline = Decimal("0.05") * 2 + Decimal(14) * Decimal("30.5") / Decimal(10000)
    alternate = Decimal("0.05") * 2 + Decimal(1) * Decimal("30.5") / Decimal(10000)
    assert result.delta == str(alternate - baseline)
    assert result.transaction_count == 2
    assert result.baseline_fee == str(baseline)


def test_counterfactual_requires_non_negative_transaction_values():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    with pytest.raises(ValueError):
        counterfactual_delta(rules[0], [Decimal("-1")], alternate_rate=Decimal(1))


def test_semantics_declaration_is_required_for_null_wildcards():
    strict_null = RuleSemantics(null_is_wildcard=False, source=SOURCE)
    with pytest.raises(RuleSourceError):
        load_rule_set(ANNEX, source=SOURCE, semantics=strict_null)


def test_unknown_observation_policy_is_declared_not_assumed():
    rules = load_rule_set(ANNEX, source=SOURCE, semantics=NULL_WILD)
    strict = next(rule for rule in rules if rule.rule_id == "B")
    skip_semantics = RuleSemantics(
        null_is_wildcard=True,
        empty_list_is_wildcard=False,
        unknown_observation="skip",
        source=SOURCE,
    )
    lenient = next(
        rule
        for rule in load_rule_set(ANNEX, source=SOURCE, semantics=skip_semantics)
        if rule.rule_id == "B"
    )
    # Rule B constrains account type, MCC and capture delay. With the default
    # reject policy those observations must be supplied.
    assert not strict.matches_facets(
        account_type=None,
        merchant_category_code=None,
        capture_delay_days=None,
        capture_delay_mode=None,
        monthly_volume=None,
        monthly_fraud_level=None,
    )
    # Numeric thresholds are never "skip"-able: a missing denominator cannot be
    # treated as any volume, so even the lenient policy rejects this rule.
    assert not lenient.matches_facets(
        account_type=None,
        merchant_category_code=None,
        capture_delay_days=None,
        capture_delay_mode=None,
        monthly_volume=None,
        monthly_fraud_level=None,
    )
    # With the numeric observations supplied, both policies agree.
    assert strict.matches_facets(
        account_type="R",
        merchant_category_code=5411,
        capture_delay_days=2,
        capture_delay_mode=None,
        monthly_volume=500_000.0,
        monthly_fraud_level=0.08,
    )
    # The lenient policy alone tolerates missing merchant attributes.
    lenient_only_rule = lenient.model_copy(
        update={"monthly_volume": None, "monthly_fraud_level": None}
    )
    assert lenient_only_rule.matches_facets(
        account_type=None,
        merchant_category_code=None,
        capture_delay_days=None,
        capture_delay_mode=None,
        monthly_volume=None,
        monthly_fraud_level=None,
    )
    strict_only_rule = strict.model_copy(
        update={"monthly_volume": None, "monthly_fraud_level": None}
    )
    assert not strict_only_rule.matches_facets(
        account_type=None,
        merchant_category_code=None,
        capture_delay_days=None,
        capture_delay_mode=None,
        monthly_volume=None,
        monthly_fraud_level=None,
    )


def test_empty_wildcard_field_does_not_require_an_observation():
    wildcard_rules = load_rule_set(ANNEX, source=SOURCE, semantics=EMPTY_WILD)
    rule_a = next(rule for rule in wildcard_rules if rule.rule_id == "A")
    # account_type and MCC are empty-as-wildcard, so they do not restrict; the
    # ACI list does, and must therefore be supplied.
    assert not rule_a.account_type.restricts
    assert rule_a.aci.restricts
    assert rule_a.matches_facets(
        account_type=None,
        merchant_category_code=None,
        capture_delay_days=None,
        capture_delay_mode=None,
        monthly_volume=None,
        monthly_fraud_level=None,
    )
