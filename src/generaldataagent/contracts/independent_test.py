"""Bounded independent-group test inputs; no generated SQL or executable code."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TestContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PublicTextFilter(TestContract):
    column: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=200)
    source_excerpt: str = Field(min_length=1, max_length=2000)


class SignificanceRule(TestContract):
    alpha: float = Field(gt=0, lt=1, allow_inf_nan=False)
    significant_label: str = Field(min_length=1, max_length=160)
    nonsignificant_label: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def distinct(self):
        if self.significant_label == self.nonsignificant_label:
            raise ValueError("significance labels must differ")
        return self


class IndependentTestSpec(TestContract):
    groups: list[str | int | float] = Field(min_length=2, max_length=2)
    filters: list[PublicTextFilter] = Field(default_factory=list, max_length=8)
    variance: Literal["student", "welch"]
    significance_rule: SignificanceRule | None = None

    @model_validator(mode="after")
    def valid_groups(self):
        if self.groups[0] == self.groups[1]:
            raise ValueError("two distinct groups required")
        if any(isinstance(v, float) and not math.isfinite(v) for v in self.groups):
            raise ValueError("finite group values required")
        if any(isinstance(v, str) and len(v) > 200 for v in self.groups) or any(
            type(v) is int and not -(2**63) <= v < 2**63 for v in self.groups
        ):
            raise ValueError("group values exceed SQLite parameter bounds")
        if len({item.column for item in self.filters}) != len(self.filters):
            raise ValueError("duplicate group filter columns")
        return self
