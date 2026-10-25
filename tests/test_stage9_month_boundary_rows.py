from decimal import Decimal

import pytest

from generaldataagent.business.rules import engine
from generaldataagent.business.rules.verification import verify_answer, verify_month_boundary
from generaldataagent.contracts.rules import FeeResult, RuleMatchReport


def rule():
    return engine.FeeRule(
        rule_id="A",
        card_scheme="Alpha",
        fixed_amount="0.05",
        rate="14",
        source="fees@sha256:" + "0" * 64,
    )


@pytest.mark.parametrize(
    "row",
    [
        {"year": 2024, "day_of_year": 60.9},
        {"year": 2024, "day_of_year": True},
        {"year": 2024.5, "day_of_year": 60},
        {"year": "2024", "day_of_year": " 60 "},
        {"day_of_year": 60},
        {"year": 2024},
        ["not", "a", "row"],
        None,
    ],
)
def test_month_boundary_rejects_rows_it_cannot_place_exactly(row):
    failures = verify_month_boundary([row], 2024, 2)
    assert failures == ("invalid_month_row:0",)


def test_month_boundary_still_catches_a_real_adjacent_month():
    assert verify_month_boundary([{"year": 2023, "day_of_year": 60}], 2023, 2) == (
        "row_outside_month:0",
    )


def test_verify_answer_fails_closed_on_an_unplaceable_month_row():
    result = FeeResult(
        rule_id="A",
        fixed_amount="0.05",
        rate="14",
        fee="0.05",
        source="fees@sha256:" + "0" * 64,
    )
    outcome = verify_answer(
        results=[result],
        transaction_value=Decimal(0),
        report=RuleMatchReport(matched=("A",), unmatched_count=0),
        citations=[],
        allow_multiple=True,
        monthly_rows=[{"year": 2024, "day_of_year": 60.9}],
        expected_month=(2024, 2),
        reported_ratio=0.0,
    )
    assert outcome.passed is False
    assert "invalid_month_row:0" in outcome.failures
