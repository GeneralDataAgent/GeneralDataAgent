"""Explicit Stage 8 methods and a bounded, non-code feature language."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from generaldataagent.contracts.data_science import SplitSpec


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DerivedFeature(Contract):
    name: str = Field(min_length=1, max_length=200)
    operation: Literal[
        "add",
        "subtract",
        "multiply",
        "divide",
        "year",
        "month",
        "day",
        "group_mean",
        "group_sum",
        "group_median",
        "group_count",
    ]
    columns: list[str] = Field(min_length=1, max_length=2)
    group_by: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def arity(self):
        binary = self.operation in {"add", "subtract", "multiply", "divide"}
        if len(self.columns) != (2 if binary else 1):
            raise ValueError("feature operator has incorrect arity")
        if bool(self.group_by) != self.operation.startswith("group_"):
            raise ValueError("group operators require group_by only")
        return self


class NumericFilter(Contract):
    column: str = Field(min_length=1, max_length=200)
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    value: float = Field(allow_inf_nan=False)


class MethodSpec(Contract):
    ddof: Literal[0, 1] = 1
    missing: Literal["omit", "raise"] = "omit"
    alpha: float = Field(default=0.05, gt=0, lt=1, allow_inf_nan=False)
    z_threshold: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    iqr_multiplier: float = Field(default=1.5, gt=0, allow_inf_nan=False)
    normality: Literal["auto", "shapiro", "dagostino"] = "auto"
    alternative: Literal["two-sided"] = "two-sided"


class StatisticsRequest(Contract):
    columns: list[str] = Field(min_length=1, max_length=20)
    operation: Literal["summary", "pearson", "normality", "outliers"] = "summary"
    method: MethodSpec = Field(default_factory=MethodSpec)
    features: list[DerivedFeature] = Field(default_factory=list, max_length=20)
    where: list[NumericFilter] = Field(default_factory=list, max_length=20)
    group_by: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def coherent(self):
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("statistics columns must be unique")
        if self.operation == "pearson" and len(self.columns) != 2:
            raise ValueError("Pearson requires two columns")
        if len({f.name for f in self.features}) != len(self.features):
            raise ValueError("derived feature names must be unique")
        return self


class RegressionRequest(Contract):
    target: str = Field(min_length=1, max_length=200)
    features: list[str] = Field(min_length=1, max_length=20)
    derived: list[DerivedFeature] = Field(default_factory=list, max_length=20)
    split: SplitSpec = Field(default_factory=SplitSpec)
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    missing_features: Literal["raise", "train_mean"] = "raise"
    standardize: bool = True

    @model_validator(mode="after")
    def safe_features(self):
        if self.target in self.features or len(set(self.features)) != len(self.features):
            raise ValueError("unique features must exclude target")
        contaminated = {self.target}
        for feature in self.derived:
            if feature.group_by:
                raise ValueError(
                    "full-population group aggregation cannot precede predictive split"
                )
            if contaminated.intersection(feature.columns):
                contaminated.add(feature.name)
        if contaminated.intersection(self.features):
            raise ValueError("derived target leakage")
        return self
