"""Shared rule application operations, independent of workflow and benchmark transport."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from generaldataagent.business.rules.engine import monthly_totals
from generaldataagent.business.rules.payments import PaymentsSourceError, moment_of
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.contracts.rules import FeeRule


def select_rule_facets(
    rules: Sequence[FeeRule], request: RuleAnalysisRequest
) -> tuple[FeeRule, ...]:
    """Filter only explicitly supplied facets, retaining source order and identity.

    Merchant/month thresholds and transaction applicability are later operations.
    Wildcard behavior is already bound into the typed rules by their loader.
    """
    return tuple(
        rule
        for rule in rules
        if (
            (request.card_scheme is None or rule.card_scheme == request.card_scheme)
            and (request.account_type is None or rule.account_type.matches(request.account_type))
            and (
                request.merchant_category_code is None
                or rule.merchant_category_code.matches(request.merchant_category_code)
            )
            and (request.aci is None or rule.aci.matches(request.aci))
            and (request.is_credit is None or rule.is_credit in (None, request.is_credit))
            and (request.intracountry is None or rule.intracountry in (None, request.intracountry))
        )
    )


@dataclass(frozen=True, slots=True)
class MonthScope:
    """One natural month's rows plus the day-scoped subset and exact totals."""

    month: int
    monthly_rows: tuple[Mapping[str, Any], ...]
    scoped_rows: tuple[Mapping[str, Any], ...]
    volume: Decimal
    fraud_volume: Decimal
    transaction_count: int
    fraud_count: int

    @property
    def fraud_ratio(self) -> Decimal | None:
        """Fraud-to-volume ratio; undefined rather than invented for zero volume."""
        return self.fraud_volume / self.volume if self.volume else None


def resolve_target_month(*, year: int, month: int | None, day_of_year: int | None) -> int:
    """Resolve a requested month or ordinal day; an invalid ordinal fails closed."""
    if day_of_year is None:
        if month is None:
            raise ValueError("month or day_of_year required")
        return month
    return moment_of({"year": year, "day_of_year": day_of_year}).month


def scope_rule_month(
    rows: Sequence[Mapping[str, Any]], *, month: int, day_of_year: int | None = None
) -> MonthScope:
    """Select the natural month and optional day, computing totals once.

    Thresholds always use the whole month even when the caller asked about one
    day. Date resolution uses the strict payments parser, so a fractional or
    boolean day is refused instead of being truncated. An empty month raises
    the source's stable ``payments_month_empty`` code; zero volume is returned
    so each consumer keeps its own declared policy and error code.
    """
    dated = tuple((row, moment_of(row)) for row in rows)
    monthly = tuple(row for row, moment in dated if moment.month == month)
    if not monthly:
        raise PaymentsSourceError("payments_month_empty")
    scoped = tuple(
        row
        for row, moment in dated
        if moment.month == month
        and (day_of_year is None or moment.timetuple().tm_yday == day_of_year)
    )
    volume, fraud_volume, count, fraud_count = monthly_totals(monthly)
    return MonthScope(
        month=month,
        monthly_rows=monthly,
        scoped_rows=scoped,
        volume=volume,
        fraud_volume=fraud_volume,
        transaction_count=count,
        fraud_count=fraud_count,
    )
