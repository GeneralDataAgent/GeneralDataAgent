"""Retrieval citations and fail-closed answer verification for stage 9."""

from decimal import Decimal
from pathlib import Path

import pytest

from generaldataagent.business.rules import (
    PassageError,
    citation,
    load_rule_set,
    match_rules,
    resolve_rule_citations,
    select_passages,
    split_passages,
    verify_answer,
    verify_band_round_trip,
    verify_citations,
    verify_fee_arithmetic,
    verify_monthly_ratio,
    verify_unique_match,
    verify_wildcard_handling,
)
from generaldataagent.contracts.rules import (
    FeeResult,
    RuleMatchReport,
    RuleSemantics,
    RuleVerification,
    TransactionFacets,
)

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "data/stage6/dabstep/manual.md"

DOCUMENT = """\
Intro text before any heading.

# Section One

Fees are fixed plus rate.

## Section Two

The rule 384 applies to credit transactions.

## Section Three

Unrelated content about chargebacks.
"""

ANNEX = [
    {
        "ID": "384",
        "card_scheme": "Alpha",
        "account_type": [],
        "capture_delay": None,
        "monthly_fraud_level": None,
        "monthly_volume": None,
        "merchant_category_code": [],
        "is_credit": True,
        "aci": None,
        "fixed_amount": 0.05,
        "rate": 14,
    }
]
SEMANTICS = RuleSemantics(
    null_is_wildcard=True, empty_list_is_wildcard=True, unknown_observation="skip", source="annex"
)


def test_passages_are_heading_delimited_and_cite_exact_lines():
    passages = split_passages(DOCUMENT, source="policy.md")
    assert [passage.heading for passage in passages] == [
        "",
        "Section One",
        "Section Two",
        "Section Three",
    ]
    assert passages[0].text == "Intro text before any heading."
    assert passages[0].start_line == 1
    assert passages[2].start_line == 8
    assert passages[2].end_line == 8
    assert citation(passages[2]) == "policy.md#Section Two:8-8"


def test_blank_source_and_oversized_passage_are_rejected():
    with pytest.raises(PassageError):
        split_passages(DOCUMENT, source="  ")
    huge = "# Big\n\n" + ("x" * 8_001)
    with pytest.raises(PassageError, match="exceeds"):
        split_passages(huge, source="big.md")


def test_selection_requires_terms_and_never_falls_back_to_whole_document():
    passages = split_passages(DOCUMENT, source="policy.md")
    selection = select_passages(passages, required_terms=["credit"])
    assert [passage.heading for passage in selection.selected] == ["Section Two"]
    assert selection.hit_count == 1

    empty = select_passages(passages, required_terms=["nonexistent"])
    assert empty.selected == ()
    assert empty.hit_count == 0

    with pytest.raises(PassageError):
        select_passages(passages, required_terms=[])
    with pytest.raises(PassageError):
        select_passages(passages, required_terms=["credit"], limit=0)


def test_selection_ranks_by_optional_terms_and_stays_bounded():
    passages = split_passages(MANUAL.read_text(encoding="utf-8"), source="manual.md")
    selection = select_passages(
        passages, required_terms=["fee"], optional_terms=["fixed_amount", "rate"], limit=3
    )
    assert len(selection.selected) <= 3
    assert selection.hit_count >= len(selection.selected)
    assert "fixed_amount" in selection.selected[0].text


def test_rule_citations_bind_ids_and_reject_uncited_rules():
    passages = split_passages(DOCUMENT, source="policy.md")
    citations = resolve_rule_citations(passages, ["384"])
    assert citations[0].reference == "policy.md#Section Two:8-8"
    with pytest.raises(PassageError, match="no citing passage"):
        resolve_rule_citations(passages, ["999"])


def test_rule_id_matching_respects_token_boundaries():
    document = "# Rules\n\nRule 1384 does not mention the other identifier.\n"
    passages = split_passages(document, source="policy.md")
    with pytest.raises(PassageError):
        resolve_rule_citations(passages, ["384"])


def test_fee_arithmetic_mismatch_is_reported_per_rule():
    good = FeeResult(rule_id="384", fixed_amount="0.05", rate="14", fee="0.064000", source="annex")
    bad = good.model_copy(update={"fee": "0.070000"})
    assert verify_fee_arithmetic([good], Decimal("10")) == ()
    assert verify_fee_arithmetic([bad], Decimal("10")) == ("fee_mismatch:384",)


def test_ambiguous_and_missing_matches_fail_closed():
    ambiguous = RuleMatchReport(matched=("1", "2"), unmatched_count=0, ambiguity=True)
    assert verify_unique_match(ambiguous, allow_multiple=False) == ("multiple_applicable_rules",)
    assert verify_unique_match(ambiguous, allow_multiple=True) == ()

    missing = RuleMatchReport(matched=(), unmatched_count=5)
    assert verify_unique_match(missing, allow_multiple=True) == ("no_applicable_rule",)

    unresolved = RuleMatchReport(matched=(), unmatched_count=4, indeterminate=("3",))
    assert "indeterminate_match" in verify_unique_match(unresolved, allow_multiple=True)


def test_monthly_ratio_uses_the_declared_denominator():
    rows = [
        {"eur_amount": "100.00", "has_fraudulent_dispute": "True"},
        {"eur_amount": "300.00", "has_fraudulent_dispute": "False"},
    ]
    by_amount = float(Decimal("100.00") / Decimal("400.00"))
    assert verify_monthly_ratio(rows, reported_ratio=by_amount, by_count=False) == ()
    by_count = 1 / 2
    assert verify_monthly_ratio(rows, reported_ratio=by_count, by_count=True) == ()
    # Reporting the count ratio while claiming the amount convention must fail.
    assert verify_monthly_ratio(rows, reported_ratio=by_count, by_count=False) != ()


def test_zero_denominator_is_explicit_not_silent():
    rows = [{"eur_amount": "0", "has_fraudulent_dispute": "True"}]
    assert verify_monthly_ratio(rows, reported_ratio=0.0, by_count=False) == (
        "ratio_denominator_zero",
    )


def test_wildcard_checks_flag_unmatchable_and_unbounded_rules():
    rules = load_rule_set(ANNEX, source="annex", semantics=SEMANTICS)
    assert verify_wildcard_handling(rules) == ()
    strict = RuleSemantics(
        null_is_wildcard=True, empty_list_is_wildcard=False, unknown_observation="skip", source="a"
    )
    strict_rules = load_rule_set(ANNEX, source="annex", semantics=strict)
    assert verify_wildcard_handling(strict_rules) == (
        "unmatchable_empty_list:384:account_type",
        "unmatchable_empty_list:384:merchant_category_code",
    )


def test_citations_must_cover_every_reported_rule():
    results = [
        FeeResult(rule_id="384", fixed_amount="0.05", rate="14", fee="0.064", source="annex")
    ]
    passages = split_passages(DOCUMENT, source="policy.md")
    citations = resolve_rule_citations(passages, ["384"])
    assert verify_citations(citations, results) == ()
    assert verify_citations((), results) == ("uncited_rule:384",)


def test_aggregate_verification_fails_closed_on_missing_month_context():
    results = [
        FeeResult(rule_id="384", fixed_amount="0.05", rate="14", fee="0.064000", source="annex")
    ]
    passages = split_passages(DOCUMENT, source="policy.md")
    citations = resolve_rule_citations(passages, ["384"])
    report = RuleMatchReport(matched=("384",), unmatched_count=0)
    ok = verify_answer(
        results=results,
        transaction_value=Decimal("10"),
        report=report,
        citations=citations,
        allow_multiple=False,
    )
    assert ok.passed and ok.failures == ()

    bad = verify_answer(
        results=results,
        transaction_value=Decimal("10"),
        report=report,
        citations=citations,
        allow_multiple=False,
        reported_ratio=0.1,
    )
    assert not bad.passed
    assert "ratio_without_month_context" in bad.failures


def test_verification_contract_rejects_incoherent_states():
    with pytest.raises(ValueError):
        RuleVerification(passed=True, failures=("x",))
    with pytest.raises(ValueError):
        RuleVerification(passed=False, failures=())


@pytest.mark.parametrize(
    "band,value",
    [
        (">8.3%", 0.10),
        ("<7.2%", 0.05),
        ("7.7%-8.3%", 0.08),
        ("100k-1m", 500_000.0),
        (">5m", 5_000_001.0),
    ],
)
def test_band_round_trip_matches_raw_interpretation(band, value):
    assert verify_band_round_trip(band, value) == ()


def test_band_round_trip_detects_disagreement(monkeypatch):
    from generaldataagent.business.rules import verification

    monkeypatch.setattr(
        verification,
        "parse_band",
        lambda text: type(
            "Band", (), {"contains": staticmethod(lambda value: True), "lower": 0, "upper": 0}
        )(),
    )
    assert verify_band_round_trip(">8.3%", 0.01) == ("band_disagreement:>8.3%",)


def test_merchant_match_end_to_end_cites_rule_and_verifies():
    rules = load_rule_set(ANNEX, source="annex", semantics=SEMANTICS)
    merchant = {
        "account_type": "R",
        "merchant_category_code": 5411,
        "capture_delay": None,
    }
    matched = match_rules(rules, merchant=merchant, monthly_volume=None, monthly_fraud_level=None)
    assert [rule.rule_id for rule in matched] == ["384"]
    transaction = TransactionFacets(
        card_scheme="Alpha", aci="A", is_credit=True, intracountry=False, amount="10"
    )
    from generaldataagent.business.rules import applicable_rules, fee_results

    report = applicable_rules(matched, [transaction])
    results = fee_results(matched, transaction_value=Decimal("10"), rounding="none")
    passages = split_passages(DOCUMENT, source="policy.md")
    citations = resolve_rule_citations(passages, report.matched)
    outcome = verify_answer(
        results=results,
        transaction_value=Decimal("10"),
        report=report,
        citations=citations,
        allow_multiple=False,
    )
    assert outcome.passed
    assert results[0].fee == "0.064"
