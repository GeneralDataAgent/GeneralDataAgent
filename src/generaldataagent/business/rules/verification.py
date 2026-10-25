"""Independent checks that a rule-derived answer is internally consistent.

The verifier does not recompute the whole pipeline. It re-derives the parts a
wrong answer usually breaks — arithmetic, wildcard interpretation, month
boundaries, multiple-match handling and missing-match behaviour — from the
matched rules and the cited passages, and reports stable failure codes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from generaldataagent.business.rules.engine import (
    RuleSourceError,
    compute_fee,
    monthly_totals,
    parse_band,
    transaction_matches,
)
from generaldataagent.business.rules.payments import PaymentsSourceError, moment_of
from generaldataagent.business.rules.retrieval import RuleCitation
from generaldataagent.contracts.rules import (
    CounterfactualResult,
    FeeResult,
    FeeRule,
    RuleMatchReport,
    RuleVerification,
    TransactionFacets,
)

TOLERANCE = Decimal("0.000001")


def _exact_decimal(value: object) -> Decimal | None:
    """Parse exact decimal text, rejecting numbers, booleans and non-finite values."""
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def verify_authoritative_fee_summary(
    rules: Sequence[FeeRule],
    transaction_value: Decimal,
    claim: Mapping[str, Any],
) -> tuple[str, ...]:
    """Re-derive a fee-summary claim from authoritative rules.

    This deliberately does not call ``fee_results``, ``average_fee`` or
    ``compute_fee``. A defect or monkeypatch in the production calculation
    therefore cannot make the verifier repeat the same answer. The returned
    issue names are transport-neutral; each consumer maps them to its stable
    public failure codes.
    """
    if not isinstance(claim, Mapping) or set(claim) != {"fees", "average_fee"}:
        return ("computation_invalid",)
    rows = claim.get("fees")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return ("records_invalid",)

    issues = []
    if [row.get("rule_id") for row in rows] != [rule.rule_id for rule in rules]:
        issues.append("result_ids_mismatch")
    expected_fees = [
        Decimal(rule.fixed_amount) + Decimal(rule.rate) * transaction_value / Decimal(10000)
        for rule in rules
    ]
    for rule, row, expected in zip(rules, rows, expected_fees, strict=False):
        if (
            set(row) != set(FeeResult.model_fields)
            or row.get("schema_version") != FeeResult.model_fields["schema_version"].default
            or row.get("rule_id") != rule.rule_id
            or row.get("fixed_amount") != rule.fixed_amount
            or row.get("rate") != rule.rate
            or row.get("source") != rule.source
            or _exact_decimal(row.get("fee")) != expected
        ):
            issues.append("record_mismatch")

    expected_average = (
        sum(expected_fees, Decimal(0)) / Decimal(len(expected_fees)) if expected_fees else None
    )
    average = claim.get("average_fee")
    if (
        average is not None
        if expected_average is None
        else _exact_decimal(average) != expected_average
    ):
        issues.append("average_mismatch")
    return tuple(dict.fromkeys(issues))


def verify_authoritative_counterfactual(
    rule: FeeRule,
    transactions: Sequence[TransactionFacets],
    *,
    alternate_rate: Decimal,
    claim: Mapping[str, Any],
) -> tuple[str, ...]:
    """Re-derive every counterfactual field without calling the producer."""
    if not isinstance(claim, Mapping) or set(claim) != set(CounterfactualResult.model_fields):
        return ("record_invalid",)
    if claim.get("schema_version") != CounterfactualResult.model_fields["schema_version"].default:
        return ("record_invalid",)

    amounts = [
        Decimal(transaction.amount)
        for transaction in transactions
        if transaction_matches(rule, transaction)
    ]
    total = sum(amounts, Decimal(0))
    fixed_total = Decimal(rule.fixed_amount) * Decimal(len(amounts))
    baseline = fixed_total + Decimal(rule.rate) * total / Decimal(10000)
    alternate = fixed_total + alternate_rate * total / Decimal(10000)
    expected = {
        "baseline_fee": baseline,
        "alternate_fee": alternate,
        "delta": alternate - baseline,
    }
    issues = []
    if claim.get("rule_id") != rule.rule_id:
        issues.append("rule_mismatch")
    for field, value in expected.items():
        if _exact_decimal(claim.get(field)) != value:
            issues.append(field.removesuffix("_fee") + "_mismatch")
    count = claim.get("transaction_count")
    if type(count) is not int or count != len(amounts):
        issues.append("transaction_count_mismatch")
    return tuple(issues)


def verify_fee_arithmetic(
    results: Sequence[FeeResult], transaction_value: Decimal
) -> tuple[str, ...]:
    """Each reported fee must equal fixed + rate * value / 10000 exactly."""
    failures = []
    for result in results:
        expected = Decimal(result.fixed_amount) + (
            Decimal(result.rate) * transaction_value / Decimal(10000)
        )
        if abs(Decimal(result.fee) - expected) > TOLERANCE:
            failures.append(f"fee_mismatch:{result.rule_id}")
    return tuple(failures)


def verify_unique_match(report: RuleMatchReport, *, allow_multiple: bool) -> tuple[str, ...]:
    """A single-answer request must not silently pick one of many matches."""
    failures = []
    if report.ambiguity and not allow_multiple:
        failures.append("multiple_applicable_rules")
    if not report.matched and not report.indeterminate:
        failures.append("no_applicable_rule")
    if report.indeterminate:
        failures.append("indeterminate_match")
    return tuple(failures)


def verify_month_boundary(
    rows: Sequence[dict], expected_year: int, expected_month: int
) -> tuple[str, ...]:
    """Every row counted in a monthly result must fall in that natural month.

    A row that cannot be placed on an exact calendar date is a verification
    failure, not an exception: this check must reject, not crash. The date is
    resolved with the same strict parser the payments source uses, so a
    fractional or boolean day cannot be silently truncated into the month.
    """
    failures = []
    for index, row in enumerate(rows):
        try:
            moment = moment_of(row)
        except (PaymentsSourceError, KeyError, TypeError):
            failures.append(f"invalid_month_row:{index}")
            continue
        if (moment.year, moment.month) != (expected_year, expected_month):
            failures.append(f"row_outside_month:{index}")
            break
    return tuple(failures)


def verify_monthly_ratio(
    rows: Sequence[dict], *, reported_ratio: float, by_count: bool
) -> tuple[str, ...]:
    """The reported ratio must match the declared denominator convention."""
    if type(by_count) is not bool:
        return ("ratio_mode_invalid",)
    # Fractions are in [0, 1]; the ordered comparison also rejects NaN and
    # infinities. Exclude bool explicitly so it cannot stand in for 0 or 1.
    if type(reported_ratio) not in (int, float) or not 0 <= reported_ratio <= 1:
        return ("ratio_value_invalid",)
    volume, fraud_volume, count, fraud_count = monthly_totals(rows)
    if by_count:
        expected = fraud_count / count if count else None
    else:
        expected = float(fraud_volume / volume) if volume else None
    if expected is None:
        return ("ratio_denominator_zero",)
    if abs(reported_ratio - expected) > float(TOLERANCE):
        return (f"ratio_mismatch:expected={expected:.12f}",)
    return ()


def verify_wildcard_handling(rules: Sequence) -> tuple[str, ...]:
    """Declared semantics must be internally coherent for every rule."""
    failures = []
    for rule in rules:
        for field in ("account_type", "merchant_category_code", "aci"):
            match = getattr(rule, field)
            if match.values is not None and not match.values and not match.empty_is_wildcard:
                # An empty list with no wildcard declaration is a rule that can
                # never match; that is usually a loader bug, not an intent.
                failures.append(f"unmatchable_empty_list:{rule.rule_id}:{field}")
        for field in ("monthly_volume", "monthly_fraud_level"):
            band = getattr(rule, field)
            if band is not None and band.lower is None and band.upper is None:
                failures.append(f"unbounded_band:{rule.rule_id}:{field}")
    return tuple(failures)


def verify_citations(
    citations: Sequence[RuleCitation], results: Sequence[FeeResult]
) -> tuple[str, ...]:
    """Every reported rule must have a passage that actually names it."""
    cited = {citation.rule_id for citation in citations}
    failures = []
    for result in results:
        if result.rule_id not in cited:
            failures.append(f"uncited_rule:{result.rule_id}")
        elif not citations:
            failures.append("missing_citation_source")
    return tuple(failures)


def verify_answer(
    *,
    results: Sequence[FeeResult],
    transaction_value: Decimal,
    report: RuleMatchReport,
    citations: Sequence[RuleCitation],
    allow_multiple: bool,
    monthly_rows: Sequence[dict] | None = None,
    expected_month: tuple[int, int] | None = None,
    reported_ratio: float | None = None,
    ratio_by_count: bool = False,
) -> RuleVerification:
    """Aggregate every check into one fail-closed verification result."""
    failures: list[str] = []
    failures.extend(verify_fee_arithmetic(results, transaction_value))
    failures.extend(verify_unique_match(report, allow_multiple=allow_multiple))
    failures.extend(verify_citations(citations, results))
    if monthly_rows is not None:
        if expected_month is None:
            failures.append("month_context_required")
        else:
            failures.extend(verify_month_boundary(monthly_rows, *expected_month))
        if reported_ratio is not None:
            try:
                failures.extend(
                    verify_monthly_ratio(
                        monthly_rows, reported_ratio=reported_ratio, by_count=ratio_by_count
                    )
                )
            except (KeyError, TypeError, ValueError):
                # A row that cannot be totalled is a verification failure, not an
                # exception: fail closed and name the check that could not run.
                failures.append("month_ratio_unverifiable")
    elif reported_ratio is not None:
        failures.append("ratio_without_month_context")
    return RuleVerification(
        passed=not failures,
        failures=tuple(failures),
        checks=(
            "fee_arithmetic",
            "unique_match",
            "citation",
            "month_boundary",
            "ratio_denominator",
        ),
    )


def verify_band_round_trip(text: str, value: float) -> tuple[str, ...]:
    """A documented band must accept a value only if the raw form does."""
    band = parse_band(text)
    if band is None:
        return ("band_missing",)
    if band.contains(value) != _raw_band_contains(text, value):
        return (f"band_disagreement:{text}",)
    return ()


def _raw_band_contains(text: str, value: float) -> bool:
    raw = text.strip()
    if raw.endswith("%"):
        raw, percent = raw[:-1], True
    else:
        raw, percent = raw, False
    raw = raw.replace("%", "")

    def number(token: str) -> float:
        token = token.strip()
        scale = 1_000_000 if token.endswith("m") else 1_000 if token.endswith("k") else 1
        if scale != 1:
            token = token[:-1]
        return float(token) * scale / (100 if percent else 1)

    if raw.startswith("<"):
        return value < number(raw[1:])
    if raw.startswith(">"):
        return value > number(raw[1:])
    low, high = raw.split("-")
    return number(low) <= value <= number(high)


def ensure_rule_source_known(rule_source: str, known_sources: Sequence[str]) -> tuple[str, ...]:
    """A computation must name an annex that was actually loaded."""
    if rule_source not in set(known_sources):
        return (f"unknown_rule_source:{rule_source}",)
    return ()


__all__ = [
    "RuleSourceError",
    "compute_fee",
    "ensure_rule_source_known",
    "verify_answer",
    "verify_authoritative_counterfactual",
    "verify_authoritative_fee_summary",
    "verify_band_round_trip",
    "verify_citations",
    "verify_fee_arithmetic",
    "verify_month_boundary",
    "verify_monthly_ratio",
    "verify_unique_match",
    "verify_wildcard_handling",
]
