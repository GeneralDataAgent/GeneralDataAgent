"""Explicit public decision policies, separate from scientific inference."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PearsonMagnitudeRule(BaseModel):
    """Public binary absolute-r policy, with no p-value significance claim."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    abs_r_threshold: float = Field(ge=0, le=1, allow_inf_nan=False)
    comparison: Literal["ge", "gt"] = "ge"
    above_label: str = Field(min_length=1, max_length=160)
    below_label: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def distinct_labels(self):
        if self.above_label == self.below_label:
            raise ValueError("magnitude labels must be distinct")
        return self


class PearsonDecisionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alpha: float = Field(gt=0, lt=1, allow_inf_nan=False)
    abs_r_threshold: float = Field(ge=0, le=1, allow_inf_nan=False)
    significant_high_label: str = Field(min_length=1, max_length=160)
    significant_low_label: str = Field(min_length=1, max_length=160)
    nonsignificant_label: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def distinct_labels(self):
        if (
            len(
                {self.significant_high_label, self.significant_low_label, self.nonsignificant_label}
            )
            != 3
        ):
            raise ValueError("decision labels must be distinct")
        return self
