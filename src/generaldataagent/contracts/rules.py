"""Rule contracts for document-derived, deterministic fee computation.

Rules are data, not code: every predicate is a typed bounded value, every rule
carries its source reference, and the matcher is driven only by the contracts.
No dataset identifier or benchmark answer may appear here.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from generaldataagent.contracts.models import ContractModel

Bound = Literal["<", "<=", ">", ">=", "between"]


class RuleSemantics(ContractModel):
    """Declared interpretation of an annex; never inferred from the data.

    Documents differ on whether ``null`` or an empty list means "any value".
    Both decisions are explicit here so a loader cannot silently widen a rule.
    ``unknown_observation`` settles the separate question of what happens when
    a merchant or transaction attribute is simply not supplied: ``skip`` means
    the facet does not constrain, ``reject`` means the rule cannot be proven to
    apply and is therefore excluded.
    """

    null_is_wildcard: bool = True
    empty_list_is_wildcard: bool = False
    unknown_observation: Literal["skip", "reject"] = "reject"
    source: str = Field(min_length=1, max_length=256)


class DocumentPassage(ContractModel):
    """One citable span of a supplied document.

    Line numbers are one-based and inclusive, and ``text`` is the exact body
    the citation refers to, so a reviewer can verify the quotation.
    """

    source: str = Field(min_length=1, max_length=256)
    heading: str = Field(default="", max_length=256)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=8_000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_line < self.start_line:
            raise ValueError("passage end line precedes start line")
        return self


class PassageSelection(ContractModel):
    """Retrieved passages plus why they were selected."""

    selected: tuple[DocumentPassage, ...]
    required_terms: tuple[str, ...]
    hit_count: int = Field(ge=0)

    @model_validator(mode="after")
    def bounded(self):
        if self.hit_count < len(self.selected):
            raise ValueError("hit count cannot be smaller than the selected set")
        return self


class NumericBand(BaseModel):
    """One bounded numeric interval, for example ``>8.3%`` or ``100k-1m``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    lower: float | None = None
    lower_inclusive: bool = True
    upper: float | None = None
    upper_inclusive: bool = True
    unit: Literal["ratio", "percent", "absolute"] = "absolute"
    source_text: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def ordered(self):
        if self.lower is None and self.upper is None:
            raise ValueError("band requires at least one bound")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("band lower bound exceeds upper bound")
        return self

    def contains(self, value: float) -> bool:
        if self.lower is not None and (
            value < self.lower or (value == self.lower and not self.lower_inclusive)
        ):
            return False
        return not (
            self.upper is not None
            and (value > self.upper or (value == self.upper and not self.upper_inclusive))
        )


class ListMatch(BaseModel):
    """A list-valued rule field.

    ``None`` means the field is absent and therefore unrestricted. An empty
    list is a rule that matches nothing unless the source document explicitly
    declares empty-as-wildcard; that decision is recorded per contract, never
    inferred by the matcher.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    values: tuple[Any, ...] | None = None
    empty_is_wildcard: bool = False

    @model_validator(mode="after")
    def coherent(self):
        if self.values is not None and self.empty_is_wildcard and self.values:
            raise ValueError("empty_is_wildcard is only meaningful for an empty list")
        return self

    def matches(self, value: Any) -> bool:
        if self.values is None:
            return True
        if not self.values:
            return self.empty_is_wildcard
        return value in self.values

    @property
    def restricts(self) -> bool:
        """True when the rule actually constrains the observed value.

        A null field and a declared empty-as-wildcard field both accept every
        value, so a caller that cannot supply that observation is not thereby
        disqualified from the rule.
        """
        return self.values is not None and (bool(self.values) or not self.empty_is_wildcard)


class CaptureDelayMatch(BaseModel):
    """Capture-delay rule expressed either as a band or a named mode."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    band: NumericBand | None = None
    mode: Literal["immediate", "manual"] | None = None
    source_text: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def one_form(self):
        if (self.band is None) == (self.mode is None):
            raise ValueError("capture delay requires exactly one of band or mode")
        return self

    def matches(self, observed: int | None, mode: str | None) -> bool:
        if self.mode is not None:
            return self.mode == mode
        return observed is not None and self.band is not None and self.band.contains(observed)


class FeeRule(ContractModel):
    """One document-derived fee rule with full source provenance."""

    rule_id: str = Field(min_length=1, max_length=128)
    card_scheme: str = Field(min_length=1, max_length=64)
    account_type: ListMatch
    capture_delay: CaptureDelayMatch | None = None
    monthly_fraud_level: NumericBand | None = None
    monthly_volume: NumericBand | None = None
    merchant_category_code: ListMatch
    is_credit: bool | None = None
    aci: ListMatch
    intracountry: bool | None = None
    fixed_amount: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$")
    rate: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$")
    source: str = Field(min_length=1, max_length=256)
    unknown_observation: Literal["skip", "reject"] = "reject"

    def matches_facets(
        self,
        *,
        account_type: str | None,
        merchant_category_code: int | None,
        capture_delay_days: int | None,
        capture_delay_mode: str | None,
        monthly_volume: float | None,
        monthly_fraud_level: float | None,
    ) -> bool:
        """Merchant-level facets; transaction facets are matched separately.

        ``unknown_observation`` decides whether a constrained facet whose
        observed value was not supplied rejects the rule or is ignored.
        ``monthly_volume`` and ``monthly_fraud_level`` are always rejected when
        constrained but unavailable, because a missing denominator cannot be
        treated as "any volume".
        """
        if account_type is None:
            if self.account_type.restricts and self.unknown_observation == "reject":
                return False
        elif not self.account_type.matches(account_type):
            return False
        if merchant_category_code is None:
            if self.merchant_category_code.restricts and self.unknown_observation == "reject":
                return False
        elif not self.merchant_category_code.matches(merchant_category_code):
            return False
        if self.capture_delay is not None:
            if capture_delay_days is None and capture_delay_mode is None:
                if self.unknown_observation == "reject":
                    return False
            elif not self.capture_delay.matches(capture_delay_days, capture_delay_mode):
                return False
        if self.monthly_volume is not None and (
            monthly_volume is None or not self.monthly_volume.contains(monthly_volume)
        ):
            return False
        return not (
            self.monthly_fraud_level is not None
            and (
                monthly_fraud_level is None
                or not self.monthly_fraud_level.contains(monthly_fraud_level)
            )
        )


class TransactionFacets(BaseModel):
    """The transaction-side fields a rule can constrain."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    card_scheme: str = Field(min_length=1, max_length=64)
    aci: str = Field(min_length=1, max_length=8)
    is_credit: bool
    intracountry: bool | None = None
    amount: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$")


class FeeRequest(ContractModel):
    """A bounded fee computation request over an explicit rule set."""

    rules_source: str = Field(min_length=1, max_length=256)
    transaction_value: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$")
    rounding: Literal["none", "currency", "six_decimals"] = "currency"
    counterfactual_rate: str | None = Field(default=None, pattern=r"^[0-9]+(\.[0-9]+)?$")
    counterfactual_rule_id: str | None = Field(default=None, max_length=128)


class FeeResult(ContractModel):
    """Deterministic fee output with every matched rule cited."""

    rule_id: str
    fixed_amount: str
    rate: str
    fee: str
    source: str

    @property
    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (self.rule_id, self.fixed_amount, self.rate, self.fee, self.source)


class RuleMatchReport(ContractModel):
    """Applied, excluded, and indeterminate rules for one matching request."""

    matched: tuple[str, ...]
    unmatched_count: int = Field(ge=0)
    indeterminate: tuple[str, ...] = ()
    ambiguity: bool = False
    notes: tuple[str, ...] = ()


class RuleVerification(ContractModel):
    """Fail-closed verification outcome with stable failure codes."""

    passed: bool
    failures: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    @model_validator(mode="after")
    def coherent(self):
        if self.passed and self.failures:
            raise ValueError("a passing verification cannot carry failures")
        if not self.passed and not self.failures:
            raise ValueError("a failed verification must name at least one failure")
        return self


class CounterfactualResult(ContractModel):
    """Delta from changing one rule's rate, in the requested direction order."""

    rule_id: str
    baseline_fee: str
    alternate_fee: str
    delta: str
    transaction_count: int = Field(ge=0)
