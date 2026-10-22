"""Deterministic qualification of the document rule engine on a public annex.

This runs the engine over an explicitly supplied annex and payment table and
compares only against values computed independently inside this script. It
never reads benchmark expected answers, never inspects a task id, and never
calls a model. It is a capability check, not a benchmark score.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from generaldataagent.business.rules import (
    applicable_rules,
    average_fee,
    counterfactual_delta,
    load_rule_set,
    match_rules,
    monthly_totals,
    transaction_matches,
)
from generaldataagent.contracts.rules import RuleSemantics, TransactionFacets

ROOT = Path(__file__).resolve().parents[1]


def _month(day_of_year: int, year: int) -> tuple[int, int]:
    moment = date(year, 1, 1) + timedelta(days=day_of_year - 1)
    return moment.year, moment.month


def _read_payments(path: Path, merchant: str) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        return [row for row in csv.DictReader(stream) if row["merchant"] == merchant]


def _transactions(rows: list[dict]) -> list[TransactionFacets]:
    return [
        TransactionFacets(
            card_scheme=row["card_scheme"],
            aci=row["aci"],
            is_credit=row["is_credit"] == "True",
            intracountry=row["issuing_country"] == row["acquirer_country"],
            amount=str(row["eur_amount"]),
        )
        for row in rows
    ]


def run(dataset: Path) -> dict:
    manual = (dataset / "manual.md").read_text(encoding="utf-8")
    if "fee = fixed_amount + rate * transaction_value / 10000" not in manual:
        raise ValueError("annex formula is not present in the supplied manual")

    # The manual's own note: a null field applies to all possible values. The
    # public annex also uses empty lists for the same meaning; that is a
    # separate declaration, recorded here explicitly rather than assumed.
    semantics = RuleSemantics(
        null_is_wildcard=True,
        empty_list_is_wildcard=True,
        # A merchant attribute the caller did not supply cannot be assumed to
        # match a constrained rule, so such rules are excluded rather than
        # silently widened.
        unknown_observation="reject",
        source="manual.md#5-understanding-payment-processing-fees",
    )
    annex = json.loads((dataset / "fees.json").read_text(encoding="utf-8"))
    rules = load_rule_set(annex, source="fees.json", semantics=semantics)
    merchants = {
        entry["merchant"]: entry
        for entry in json.loads((dataset / "merchant_data.json").read_text(encoding="utf-8"))
    }
    acquirers = {
        row["acquirer"]: row["country_code"]
        for row in csv.DictReader((dataset / "acquirer_countries.csv").open(encoding="utf-8"))
    }

    checks = []

    # Facet-only selection: every rule whose merchant facets match an explicit
    # account type and ACI, with no transaction or month context. The expected
    # set supplies the merchant-level facets the engine declares as required,
    # because an observation the caller never provided is rejected by design.
    expected_ids = {
        str(entry["ID"])
        for entry in annex
        if _raw_list_matches(entry["account_type"], "R")
        and _raw_list_matches(entry["aci"], "B")
        and _raw_list_matches(entry["merchant_category_code"], 5411)
        and _raw_capture_matches(entry["capture_delay"], 1)
        and _raw_band_matches(entry["monthly_volume"], 500_000.0)
        and _raw_band_matches(entry["monthly_fraud_level"], 0.02)
    }
    got_ids = {
        rule.rule_id
        for rule in rules
        if rule.matches_facets(
            account_type="R",
            merchant_category_code=5411,
            capture_delay_days=1,
            capture_delay_mode=None,
            monthly_volume=500_000.0,
            monthly_fraud_level=0.02,
        )
        and rule.aci.matches("B")
    }
    checks.append(
        {
            "name": "facet_selection_with_declared_empty_wildcard",
            "expected": len(expected_ids),
            "observed": len(got_ids),
            "passed": got_ids == expected_ids,
        }
    )

    # Average fee across every rule that matches the stated card scheme and a
    # credit-only requirement, independent of any merchant context.
    credit_rules = tuple(
        rule for rule in rules if rule.card_scheme == "GlobalCard" and rule.is_credit is not False
    )
    observed = average_fee(credit_rules, transaction_value=Decimal("10"))
    expected = _mean_raw_fee(
        [
            entry
            for entry in annex
            if entry["card_scheme"] == "GlobalCard" and entry["is_credit"] is not False
        ],
        Decimal("10"),
        6,
    )
    checks.append(
        {
            "name": "average_fee_over_matched_scheme_rules",
            "expected": str(expected),
            "observed": str(observed),
            "passed": observed == expected,
        }
    )

    # Merchant-context selection: account type plus MCC description resolved
    # through the annex CSV, still without any month. The merchant's own capture
    # delay is supplied, because omitting it now rejects delayed rules.
    mcc_lookup = {
        int(row["mcc"]): row["description"]
        for row in csv.DictReader((dataset / "merchant_category_codes.csv").open(encoding="utf-8"))
    }
    mcc_code = next(
        code for code, text in mcc_lookup.items() if text == "Eating Places and Restaurants"
    )
    h_rules = tuple(
        rule
        for rule in rules
        if rule.card_scheme == "GlobalCard"
        and rule.matches_facets(
            account_type="H",
            merchant_category_code=mcc_code,
            capture_delay_days=2,
            capture_delay_mode=None,
            monthly_volume=500_000.0,
            monthly_fraud_level=0.02,
        )
    )
    observed = average_fee(h_rules, transaction_value=Decimal("10"))
    expected = _mean_raw_fee(
        [
            entry
            for entry in annex
            if entry["card_scheme"] == "GlobalCard"
            and _raw_list_matches(entry["account_type"], "H")
            and _raw_list_matches(entry["merchant_category_code"], mcc_code)
            and _raw_capture_days_match(entry["capture_delay"], "2")
            and _raw_band_matches(entry["monthly_volume"], 500_000.0)
            and _raw_band_matches(entry["monthly_fraud_level"], 0.02)
        ],
        Decimal("10"),
        6,
    )
    checks.append(
        {
            "name": "merchant_facet_average_fee",
            "expected": str(expected),
            "observed": str(observed),
            "rules": len(h_rules),
            "passed": observed == expected,
        }
    )

    # Month-scoped rule application. The expected set is derived directly from
    # the raw annex and rows, independently of the engine's matching helpers.
    merchant = merchants["Belles_cookbook_store"]
    rows = _read_payments(dataset / "payments.csv", merchant["merchant"])
    march = [row for row in rows if _month(int(row["day_of_year"]), int(row["year"])) == (2023, 3)]
    volume, fraud_volume, _, _ = monthly_totals(march)
    fraud_ratio = float(fraud_volume / volume)
    matched = match_rules(
        rules,
        merchant=merchant,
        monthly_volume=float(volume),
        monthly_fraud_level=fraud_ratio,
    )
    report = applicable_rules(matched, _transactions(march))
    expected_ids = {
        str(entry["ID"])
        for entry in annex
        if _raw_merchant_rule_applies(entry, merchant, float(volume), fraud_ratio, march)
    }
    checks.append(
        {
            "name": "month_scoped_rule_application",
            "expected": len(expected_ids),
            "observed": len(report.matched),
            "indeterminate": len(report.indeterminate),
            "passed": set(report.matched) == expected_ids and not report.indeterminate,
        }
    )

    # Counterfactual: change one rule's rate and recompute only the affected
    # transactions. The independent value is computed directly from the rows.
    target = next(rule for rule in rules if rule.rule_id == "384")
    affected = [
        Decimal(row["eur_amount"])
        for row in rows
        if _month(int(row["day_of_year"]), int(row["year"])) == (2023, 1)
        and transaction_matches(target, _with_domesticity(row))
    ]
    result = counterfactual_delta(target, affected, alternate_rate=Decimal(1))
    expected_delta = Decimal(1 - Decimal(target.rate)) * sum(affected, Decimal(0)) / Decimal(10000)
    checks.append(
        {
            "name": "counterfactual_rate_delta",
            "expected": str(expected_delta),
            "observed": result.delta,
            "transactions": result.transaction_count,
            "passed": Decimal(result.delta) == expected_delta,
        }
    )

    # Fraud ratio reporting: amount ratio and count ratio must differ.
    _, _, count, fraud_count = monthly_totals(
        [row for row in rows if _month(int(row["day_of_year"]), int(row["year"])) == (2023, 1)]
    )
    checks.append(
        {
            "name": "amount_and_count_ratios_are_distinct",
            "expected": True,
            "observed": round(float(fraud_volume / volume), 12) != round(fraud_count / count, 12),
            "passed": abs(float(fraud_volume / volume) - fraud_count / count) > 1e-9,
        }
    )

    return {
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
        "model_calls": 0,
        "annex_rules": len(rules),
        "acquirer_country_present": "US" in set(acquirers.values()),
    }


def _raw_list_matches(values, expected) -> bool:
    """Independent reading of the annex: null and empty both mean any value."""
    if values is None or values == []:
        return True
    return expected in values


def _raw_band_matches(text, value: float) -> bool:
    """Independent reading of a documented numeric band."""
    if text is None:
        return True
    is_percent = text.endswith("%")
    raw = text.replace("%", "")
    scale = 100 if is_percent else 1

    def number(token: str) -> float:
        token = token.strip()
        if token.endswith("k"):
            return float(token[:-1]) * 1_000
        if token.endswith("m"):
            return float(token[:-1]) * 1_000_000
        return float(token)

    if raw.startswith("<"):
        return value < number(raw[1:]) / scale
    if raw.startswith(">"):
        return value > number(raw[1:]) / scale
    low, high = raw.split("-")
    return number(low) / scale <= value <= number(high) / scale


def _raw_merchant_rule_applies(entry, merchant, volume, fraud_ratio, rows) -> bool:
    if not _raw_list_matches(entry["account_type"], merchant["account_type"]):
        return False
    if not _raw_list_matches(entry["merchant_category_code"], merchant["merchant_category_code"]):
        return False
    if not _raw_capture_days_match(entry["capture_delay"], merchant["capture_delay"]):
        return False
    if not _raw_band_matches(entry["monthly_volume"], volume):
        return False
    if not _raw_band_matches(entry["monthly_fraud_level"], fraud_ratio):
        return False
    for row in rows:
        domestic = row["issuing_country"] == row["acquirer_country"]
        if entry["card_scheme"] != row["card_scheme"]:
            continue
        if entry["is_credit"] is not None and bool(entry["is_credit"]) != (
            row["is_credit"] == "True"
        ):
            continue
        if entry["intracountry"] is not None and bool(entry["intracountry"]) != domestic:
            continue
        if not _raw_list_matches(entry["aci"], row["aci"]):
            continue
        return True
    return False


def _raw_capture_days_match(rule_value, merchant_value) -> bool:
    """Compare the merchant's capture delay, which may be a literal day count."""
    if rule_value is None:
        return True
    try:
        days = int(str(merchant_value).strip())
    except (TypeError, ValueError):
        return rule_value == merchant_value
    return _raw_capture_matches(rule_value, days)


def _raw_capture_matches(rule_value, days: int) -> bool:
    """Independent reading of the capture-delay annex values."""
    if rule_value is None:
        return True
    if not isinstance(rule_value, str) or rule_value in {"immediate", "manual"}:
        return False
    if rule_value.startswith("<"):
        return days < int(rule_value[1:])
    if rule_value.startswith(">"):
        return days > int(rule_value[1:])
    low, high = rule_value.split("-")
    return int(low) <= days <= int(high)


def _mean_raw_fee(entries, value: Decimal, places: int) -> Decimal:
    total = sum(
        (
            Decimal(str(entry["fixed_amount"]))
            + Decimal(str(entry["rate"])) * value / Decimal(10000)
            for entry in entries
        ),
        Decimal(0),
    )
    return (total / Decimal(len(entries))).quantize(Decimal(1).scaleb(-places))


def _with_domesticity(row: dict) -> TransactionFacets:
    return TransactionFacets(
        card_scheme=row["card_scheme"],
        aci=row["aci"],
        is_credit=row["is_credit"] == "True",
        intracountry=row["issuing_country"] == row["acquirer_country"],
        amount=str(row["eur_amount"]),
    )


def _applies(rule, transactions) -> bool:
    return any(transaction_matches(rule, transaction) for transaction in transactions)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/stage6/dabstep")
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".artifacts/stage9/rules-qualification"
    )
    args = parser.parse_args()
    report = run(args.dataset)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
