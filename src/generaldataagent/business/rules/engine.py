"""Deterministic rule matching and fee computation for document-derived rules.

The engine is deliberately source-agnostic: a rule set is loaded from an
explicit annex, the wildcard policy comes from a declared ``RuleSemantics``
object rather than being assumed, and every result cites the rule's source.
No benchmark identifier, question text, or expected answer can reach this
module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

from generaldataagent.contracts.rules import (
    CaptureDelayMatch,
    CounterfactualResult,
    FeeResult,
    FeeRule,
    ListMatch,
    NumericBand,
    RuleMatchReport,
    RuleSemantics,
    TransactionFacets,
)

_BAND = re.compile(
    r"^(?P<op>[<>]=?)?(?P<low>-?[0-9.]+[kmb]?)?(?:-(?P<high>-?[0-9.]+[kmb]?)?)?$",
    re.IGNORECASE,
)
_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_CURRENCY_QUANTUM = Decimal("0.01")
_SIX_QUANTUM = Decimal("0.000001")


class RuleSourceError(ValueError):
    """Raised when an annex cannot be interpreted under the declared semantics."""


def parse_band(text: str | None, *, unit: str = "absolute") -> NumericBand | None:
    """Parse a documented band such as ``>8.3%``, ``100k-1m``, or ``<7.2%``.

    Percent bands are converted to a 0-1 ratio so callers compare against a
    ratio, never a percentage. Unit suffixes multiply absolute values.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        raise RuleSourceError("band text must not be blank")
    is_percent = raw.endswith("%")
    if is_percent:
        raw = raw[:-1]
        if unit == "absolute":
            unit = "percent"
        # A range such as "7.7%-8.3%" percent-signs each bound; normalize the
        # trailing marker only, leaving the separator inside the band.
        raw = raw.replace("%", "")
    match = _BAND.match(raw)
    if match is None:
        raise RuleSourceError(f"unrecognized band {text!r}")

    def number(token: str | None) -> float | None:
        if token is None or token == "":
            return None
        if is_percent:
            return float(token) / 100
        suffix = token[-1].lower()
        if suffix in _SCALE:
            return float(token[:-1]) * _SCALE[suffix]
        return float(token)

    operator = match.group("op")
    bound = number(match.group("low"))
    high = number(match.group("high"))
    if operator is not None:
        if bound is None or high is not None:
            raise RuleSourceError(f"operator band {text!r} must have one bound")
        return NumericBand(
            lower=bound if operator.startswith(">") else None,
            lower_inclusive=operator.endswith("="),
            upper=bound if operator.startswith("<") else None,
            upper_inclusive=operator.endswith("="),
            unit=unit if unit != "absolute" else "absolute",
            source_text=text,
        )
    if bound is None:
        raise RuleSourceError(f"band {text!r} has no numeric bound")
    return NumericBand(
        lower=bound,
        upper=high if high is not None else bound,
        unit=unit if unit != "absolute" else "absolute",
        source_text=text,
    )


def parse_capture_delay(text: str | None) -> CaptureDelayMatch | None:
    """Parse the capture-delay annex values, including named modes."""
    if text is None:
        return None
    value = text.strip()
    if value in {"immediate", "manual"}:
        return CaptureDelayMatch(mode=value, source_text=text)  # type: ignore[arg-type]
    return CaptureDelayMatch(band=parse_band(value), source_text=text)


def list_match(values: Iterable[object] | None, semantics: RuleSemantics) -> ListMatch:
    """Build a list matcher; empty-list behavior comes from the declaration."""
    if values is None:
        return ListMatch(values=None, empty_is_wildcard=False)
    materialized = tuple(values)
    if not materialized and not semantics.empty_list_is_wildcard:
        return ListMatch(values=(), empty_is_wildcard=False)
    wildcard_empty = semantics.empty_list_is_wildcard and not materialized
    return ListMatch(values=materialized, empty_is_wildcard=wildcard_empty)


def load_rule_set(
    annex: Sequence[Mapping[str, object]],
    *,
    source: str,
    semantics: RuleSemantics,
    numeric_fields: Mapping[str, str] | None = None,
) -> tuple[FeeRule, ...]:
    """Translate a document annex into typed rules under explicit semantics.

    ``numeric_fields`` maps annex fields to ``"ratio"`` or ``"absolute"`` so the
    loader never guesses whether a value is a percentage or an amount.
    """
    if not semantics.null_is_wildcard:
        raise RuleSourceError("this rule engine requires null-as-wildcard semantics")
    fields = {"monthly_fraud_level": "ratio", "monthly_volume": "absolute"}
    fields.update(numeric_fields or {})
    rules: list[FeeRule] = []
    seen: set[str] = set()
    for entry in annex:
        rule_id = str(entry["ID"])
        if rule_id in seen:
            raise RuleSourceError(f"duplicate rule id {rule_id!r}")
        seen.add(rule_id)
        rules.append(
            FeeRule(
                rule_id=rule_id,
                card_scheme=str(entry["card_scheme"]),
                account_type=list_match(entry.get("account_type"), semantics),
                capture_delay=parse_capture_delay(_text(entry.get("capture_delay"))),
                monthly_fraud_level=parse_band(
                    _text(entry.get("monthly_fraud_level")),
                    unit=fields["monthly_fraud_level"],
                ),
                monthly_volume=parse_band(
                    _text(entry.get("monthly_volume")), unit=fields["monthly_volume"]
                ),
                merchant_category_code=list_match(entry.get("merchant_category_code"), semantics),
                is_credit=_bool_or_none(entry.get("is_credit")),
                aci=list_match(entry.get("aci"), semantics),
                intracountry=_bool_or_none(entry.get("intracountry")),
                fixed_amount=_decimal_text(entry.get("fixed_amount")),
                rate=_decimal_text(entry.get("rate")),
                source=source,
                unknown_observation=semantics.unknown_observation,
            )
        )
    return tuple(rules)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in (0, 1):
        return bool(value)
    raise RuleSourceError(f"expected a boolean or null, received {value!r}")


def _decimal_text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise RuleSourceError("amounts must be numeric, not boolean or null")
    text = str(value)
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        raise RuleSourceError(f"amount {value!r} is not a non-negative decimal")
    return text


def match_rules(
    rules: Sequence[FeeRule],
    *,
    merchant: Mapping[str, object],
    monthly_volume: float | None,
    monthly_fraud_level: float | None,
) -> tuple[FeeRule, ...]:
    """Return merchant-level matches only; transaction facets are applied later."""
    account_type = merchant.get("account_type")
    mcc = merchant.get("merchant_category_code")
    if account_type is not None and not isinstance(account_type, str):
        raise RuleSourceError("merchant account_type must be a string")
    if mcc is not None and not isinstance(mcc, int):
        raise RuleSourceError("merchant_category_code must be an integer")
    observed_days, observed_mode = _capture_observation(merchant.get("capture_delay"))
    return tuple(
        rule
        for rule in rules
        if rule.matches_facets(
            account_type=account_type,
            merchant_category_code=mcc,
            capture_delay_days=observed_days,
            capture_delay_mode=observed_mode,
            monthly_volume=monthly_volume,
            monthly_fraud_level=monthly_fraud_level,
        )
    )


def _capture_observation(value: object) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if text in {"immediate", "manual"}:
        return None, text
    try:
        return int(text), None
    except ValueError as exc:
        raise RuleSourceError(f"capture delay {value!r} is not a day count or mode") from exc


def transaction_matches(rule: FeeRule, transaction: TransactionFacets) -> bool:
    """Apply transaction-side facets; a None rule field is unrestricted."""
    if rule.card_scheme != transaction.card_scheme:
        return False
    if not rule.aci.matches(transaction.aci):
        return False
    if rule.is_credit is not None and rule.is_credit != transaction.is_credit:
        return False
    if rule.intracountry is not None:
        if transaction.intracountry is None:
            return False
        if rule.intracountry != transaction.intracountry:
            return False
    return True


def applicable_rules(
    rules: Sequence[FeeRule], transactions: Sequence[TransactionFacets]
) -> RuleMatchReport:
    """Rules that match at least one transaction, reported deterministically."""
    matched: list[str] = []
    indeterminate: list[str] = []
    for rule in rules:
        verdicts = []
        for transaction in transactions:
            if rule.intracountry is not None and transaction.intracountry is None:
                verdicts.append(None)
            else:
                verdicts.append(transaction_matches(rule, transaction))
        if any(verdict is True for verdict in verdicts):
            matched.append(rule.rule_id)
        elif any(verdict is None for verdict in verdicts):
            indeterminate.append(rule.rule_id)
    notes: list[str] = []
    if indeterminate:
        notes.append("intracountry unknown for at least one transaction; rule left indeterminate")
    return RuleMatchReport(
        matched=tuple(matched),
        unmatched_count=len(rules) - len(matched) - len(indeterminate),
        indeterminate=tuple(indeterminate),
        ambiguity=len(matched) > 1,
        notes=tuple(notes),
    )


def compute_fee(rule: FeeRule, transaction_value: Decimal) -> Decimal:
    """``fixed_amount + rate * transaction_value / 10000`` in exact decimal."""
    if transaction_value < 0:
        raise ValueError("transaction value must be non-negative")
    return Decimal(rule.fixed_amount) + Decimal(rule.rate) * transaction_value / Decimal(10000)


def quantize(value: Decimal, rounding: str) -> Decimal:
    if rounding == "none":
        return value
    if rounding == "currency":
        return value.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
    if rounding == "six_decimals":
        return value.quantize(_SIX_QUANTUM, rounding=ROUND_HALF_UP)
    raise ValueError(f"unsupported rounding {rounding!r}")


def fee_results(
    rules: Sequence[FeeRule],
    *,
    transaction_value: Decimal,
    rounding: str = "currency",
) -> tuple[FeeResult, ...]:
    return tuple(
        FeeResult(
            rule_id=rule.rule_id,
            fixed_amount=rule.fixed_amount,
            rate=rule.rate,
            fee=str(quantize(compute_fee(rule, transaction_value), rounding)),
            source=rule.source,
        )
        for rule in rules
    )


def average_fee(
    rules: Sequence[FeeRule], *, transaction_value: Decimal, rounding: str = "six_decimals"
) -> Decimal | None:
    """Mean fee across applicable rules; ``None`` when no rule applies."""
    if not rules:
        return None
    total = sum((compute_fee(rule, transaction_value) for rule in rules), Decimal(0))
    return quantize(total / Decimal(len(rules)), rounding)


def monthly_totals(
    rows: Iterable[Mapping[str, object]],
    *,
    amount_field: str = "eur_amount",
    fraud_field: str = "has_fraudulent_dispute",
) -> tuple[Decimal, Decimal, int, int]:
    """Return total volume, fraud volume, transaction count and fraud count."""
    volume = Decimal(0)
    fraud_volume = Decimal(0)
    count = 0
    fraud_count = 0
    for row in rows:
        amount = Decimal(str(row[amount_field]))
        if amount < 0:
            raise ValueError("amounts must be non-negative")
        volume += amount
        count += 1
        if _truthy(row.get(fraud_field)):
            fraud_volume += amount
            fraud_count += 1
    return volume, fraud_volume, count, fraud_count


def volume_ratio(volume: Decimal, fraud_volume: Decimal, *, by_count: bool = False) -> float | None:
    """Fraud ratio as a fraction; ``None`` when the denominator is zero."""
    if by_count:
        raise ValueError("count ratios require the count helper")
    if volume == 0:
        return None
    return float(fraud_volume / volume)


def _truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no", ""}:
        return False
    raise ValueError(f"unrecognized boolean literal {value!r}")


def counterfactual_delta(
    rule: FeeRule,
    transactions: Sequence[Decimal],
    *,
    alternate_rate: Decimal,
    rounding: str = "none",
) -> CounterfactualResult:
    """Fee change when one rule's rate changes, over the transactions it applies to.

    Only the rate differs; the fixed amount is still charged per transaction.
    """
    baseline = sum((compute_fee(rule, value) for value in transactions), Decimal(0))
    # Fixed amount is charged once per transaction, exactly as in the baseline.
    fixed_total = Decimal(rule.fixed_amount) * Decimal(len(transactions))
    alternate = fixed_total + alternate_rate * sum(transactions, Decimal(0)) / Decimal(10000)
    delta = alternate - baseline
    return CounterfactualResult(
        rule_id=rule.rule_id,
        baseline_fee=str(quantize(baseline, rounding)),
        alternate_fee=str(quantize(alternate, rounding)),
        delta=str(quantize(delta, rounding)),
        transaction_count=len(transactions),
    )
