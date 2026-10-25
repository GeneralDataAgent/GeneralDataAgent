from fractions import Fraction

import pytest

from generaldataagent.evaluation.rule_qualification import (
    context_cases,
    independent_context_expected,
    independent_expected,
    qualify_context,
    qualify_rules,
)


def test_raw_oracle_null_empty_and_decimal_arithmetic():
    annex = [
        dict(ID="unrestricted", account_type=None, fixed_amount="0.01", rate="3"),
        dict(ID="empty", account_type=[], fixed_amount="0.02", rate="5"),
        dict(ID="other", account_type=["other"], fixed_amount="1", rate="9"),
    ]
    request = dict(account_type="retail", transaction_value="12.50", empty_list_is_wildcard=False)
    ids, mean = independent_expected(annex, request)
    assert ids == ["unrestricted"]
    assert mean == Fraction("0.01375")
    ids, mean = independent_expected(annex, request | {"empty_list_is_wildcard": True})
    assert ids == ["unrestricted", "empty"]
    assert mean == Fraction("0.02")


def test_synthetic_suite_uses_real_workflow_and_retains_full_corpus(tmp_path):
    output = tmp_path / "qualification"
    report = qualify_rules(output, count=12)
    assert report["n"] == report["passed"] == 12
    assert report["model_calls"] == 0
    assert report["production_accepted"] is False
    assert (output / "cases.json").is_file()
    assert (output / "checkpoints.sqlite").is_file()
    with pytest.raises(FileExistsError):
        qualify_rules(output, count=12)


def test_qualification_retains_cases_when_handler_raises(tmp_path, monkeypatch):
    from generaldataagent.business.rules.workflow import AnnexWorkflowServices

    def broken(self, state):
        raise RuntimeError("synthetic fault")

    monkeypatch.setattr(AnnexWorkflowServices, "_compute", broken)
    report = qualify_rules(tmp_path / "faults", count=6)
    assert report["passed"] == 0
    assert report["n"] == len(report["results"]) == 6
    assert all(row["error_type"] == "RuntimeError" for row in report["results"])


def test_qualification_detects_corrupted_engine_average(tmp_path, monkeypatch):
    from generaldataagent.business.rules.workflow import AnnexWorkflowServices

    original = AnnexWorkflowServices._compute

    def corrupt(self, state):
        result = original(self, state)
        result["average_fee"] = "99999"
        return result

    monkeypatch.setattr(AnnexWorkflowServices, "_compute", corrupt)
    report = qualify_rules(tmp_path / "corrupt", count=6)
    assert report["passed"] == 0
    assert report["n"] == len(report["results"]) == 6


def test_context_oracle_uses_full_month_for_day_scoped_answers():
    merchant = dict(
        merchant="Shop", account_type="R", merchant_category_code=5411, capture_delay="1"
    )
    payments = [
        dict(
            merchant="Shop",
            year=2023,
            day_of_year=day,
            eur_amount=amount,
            has_fraudulent_dispute=fraud,
            card_scheme="Alpha",
            aci="A",
            is_credit=True,
            issuing_country="NL",
            acquirer_country="NL",
        )
        for day, amount, fraud in [(1, 100, True), (2, 300, False)]
    ]
    annex = [
        dict(
            ID="vol",
            card_scheme="Alpha",
            monthly_volume=">300",
            fixed_amount="0.10",
            rate="20",
        )
    ]
    # The single day is 100, but the natural month is 400 and its fraud ratio 25%.
    ids, change = independent_context_expected(
        merchant,
        payments,
        annex,
        dict(operation="applicable", year=2023, day_of_year=1),
    )
    assert ids == ["vol"]
    assert change is None


def test_context_oracle_reports_counterfactual_delta_as_exact_fraction():
    merchant = dict(
        merchant="Shop", account_type="R", merchant_category_code=5411, capture_delay="1"
    )
    payments = [
        dict(
            merchant="Shop",
            year=2023,
            day_of_year=1,
            eur_amount=200,
            has_fraudulent_dispute=False,
            card_scheme="Alpha",
            aci="A",
            is_credit=True,
            issuing_country="NL",
            acquirer_country="NL",
        )
    ]
    annex = [dict(ID="r", card_scheme="Alpha", fixed_amount="0.50", rate="10")]
    ids, change = independent_context_expected(
        merchant,
        payments,
        annex,
        dict(operation="counterfactual", year=2023, month=1, rule_id="r", alternate_rate="30"),
    )
    assert ids == ["r"]
    assert change["baseline_fee"] == Fraction("0.70")
    assert change["alternate_fee"] == Fraction("1.10")
    assert change["delta"] == Fraction("0.40")


def test_context_suite_passes_through_real_workflow_and_exercises_both_outcomes(tmp_path):
    report = qualify_context(tmp_path / "context", count=12)
    assert report["n"] == report["passed"] == 12
    assert report["production_accepted"] is False
    assert report["model_calls"] == 0
    assert (tmp_path / "context" / "report.json").is_file()
    # Both a computed delta and a predicted refusal must appear, otherwise the
    # suite could pass without ever running the success path.
    statuses = {row.get("status") or "refusal" for row in report["results"]}
    assert statuses == {"completed", "refusal"}
    assert any(
        (row.get("observed_change") or {}).get("transaction_count") is not None
        for row in report["results"]
    )


def test_context_cases_only_request_populated_months():
    from datetime import date, timedelta

    for case in context_cases(count=16):
        months = {
            (date(2023, 1, 1) + timedelta(days=int(row["day_of_year"]) - 1)).month
            for row in case["payments"]
        }
        request = case["request"]
        if "month" in request:
            assert request["month"] in months
