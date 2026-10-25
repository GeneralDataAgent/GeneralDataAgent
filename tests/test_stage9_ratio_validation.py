from decimal import Decimal

import pytest

from generaldataagent.business.rules.verification import verify_answer, verify_monthly_ratio
from generaldataagent.contracts.rules import RuleMatchReport

ROWS = [
    {"year": 2024, "day_of_year": 1, "eur_amount": "100", "has_fraudulent_dispute": "True"},
    {"year": 2024, "day_of_year": 2, "eur_amount": "300", "has_fraudulent_dispute": "False"},
]


@pytest.mark.parametrize(
    "reported", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1, True, False, "0.25"]
)
def test_ratio_verifier_requires_a_finite_fraction(reported):
    assert verify_monthly_ratio(ROWS, reported_ratio=reported, by_count=False) == (
        "ratio_value_invalid",
    )


@pytest.mark.parametrize("by_count", [None, 0, 1, "False", "amount", [], {}])
def test_ratio_verifier_requires_an_explicit_boolean_mode(by_count):
    assert verify_monthly_ratio(ROWS, reported_ratio=0.5, by_count=by_count) == (
        "ratio_mode_invalid",
    )


@pytest.mark.parametrize(
    ("reported", "by_count", "expected"),
    [(float("nan"), False, "ratio_value_invalid"), (0.5, "False", "ratio_mode_invalid")],
)
def test_aggregate_verification_cannot_pass_an_invalid_ratio_declaration(
    reported, by_count, expected
):
    outcome = verify_answer(
        results=[],
        transaction_value=Decimal(0),
        report=RuleMatchReport(matched=("A",), unmatched_count=0),
        citations=[],
        allow_multiple=True,
        monthly_rows=ROWS,
        expected_month=(2024, 1),
        reported_ratio=reported,
        ratio_by_count=by_count,
    )
    assert outcome.passed is False
    assert outcome.failures == (expected,)


@pytest.mark.parametrize(("reported", "by_count"), [(0.25, False), (0.5, True), (0.2500005, False)])
def test_valid_modes_and_existing_ratio_tolerance_are_preserved(reported, by_count):
    assert verify_monthly_ratio(ROWS, reported_ratio=reported, by_count=by_count) == ()


@pytest.mark.parametrize("reported", [0, 1, 0.0, 1.0])
def test_valid_fraction_endpoints_are_preserved(reported):
    rows = [{"eur_amount": "10", "has_fraudulent_dispute": bool(reported)}]
    assert verify_monthly_ratio(rows, reported_ratio=reported, by_count=False) == ()
