"""Bounded public tool request for annex-backed fee analysis."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


class RuleAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation: Literal["select", "average_fee", "applicable", "counterfactual"]
    rules_table: str = Field(default="fees", pattern=r"^[A-Za-z_][A-Za-z_0-9]*$")
    empty_list_is_wildcard: bool = False
    semantics_excerpt: str = Field(min_length=1, max_length=2000)
    card_scheme: str | None = Field(default=None, max_length=64)
    account_type: str | None = Field(default=None, max_length=64)
    merchant_category_code: int | None = None
    aci: str | None = Field(default=None, max_length=8)
    is_credit: bool | None = None
    intracountry: bool | None = None
    transaction_value: str = Field(default="10", pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=40)
    merchant: str | None = Field(default=None, max_length=200)
    year: int | None = Field(default=None, ge=1900, le=2200)
    month: int | None = Field(default=None, ge=1, le=12)
    day_of_year: int | None = Field(default=None, ge=1, le=366)
    rule_id: str | None = Field(default=None, max_length=128)
    alternate_rate: str | None = Field(default=None, pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=40)

    @model_validator(mode="after")
    def coherent(self):
        contextual = self.operation in {"applicable", "counterfactual"}
        if contextual and (self.merchant is None or self.year is None):
            raise PydanticCustomError(
                "rule_context_required", "merchant and year required for contextual rule execution"
            )
        if contextual and (self.month is None) == (self.day_of_year is None):
            raise PydanticCustomError(
                "rule_time_scope_required", "specify exactly one natural month or day of year"
            )
        if not contextual and any(
            value is not None for value in (self.merchant, self.year, self.month, self.day_of_year)
        ):
            raise PydanticCustomError(
                "rule_context_operation_required", "use applicable for merchant/time context"
            )
        if self.operation == "counterfactual":
            if self.rule_id is None or self.alternate_rate is None:
                raise PydanticCustomError(
                    "rule_rate_change_required",
                    "counterfactual requires rule_id and alternate_rate",
                )
        elif self.rule_id is not None or self.alternate_rate is not None:
            raise PydanticCustomError(
                "rule_counterfactual_operation_required",
                "rate changes require counterfactual operation",
            )
        return self
