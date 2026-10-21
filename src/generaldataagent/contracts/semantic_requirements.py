"""Executable semantic requirements, each anchored to an exact public excerpt."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_excerpt: str = Field(min_length=1, max_length=2000)
    scope: str = Field(
        default="root",
        min_length=1,
        max_length=200,
        description="root SELECT, named CTE/subquery alias, or unique direct physical root source; "
        "never a nested table occurrence or an ambiguous self-join source",
    )


class PredicateRequirement(Requirement):
    expression: str = Field(min_length=1, max_length=3000)
    clause: Literal["where", "having", "join"] = "where"


class DateRequirement(Requirement):
    column: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    start_inclusive: bool = True
    end_inclusive: bool = False
    encoding: Literal["iso_date", "compact_text", "compact_integer"] = "iso_date"

    @model_validator(mode="after")
    def ordered(self):
        if date.fromisoformat(self.start) > date.fromisoformat(self.end):
            raise ValueError("inverted date interval")
        return self


class RatioRequirement(Requirement):
    output_column: str = Field(min_length=1, max_length=200)
    numerator: str = Field(min_length=1, max_length=3000)
    denominator: str = Field(min_length=1, max_length=3000)
    multiplier: Literal[1, 100] = 1
    zero_guard: bool = True


class SemanticRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    predicates: list[PredicateRequirement] = Field(default_factory=list, max_length=30)
    dates: list[DateRequirement] = Field(default_factory=list, max_length=10)
    ratios: list[RatioRequirement] = Field(default_factory=list, max_length=10)
