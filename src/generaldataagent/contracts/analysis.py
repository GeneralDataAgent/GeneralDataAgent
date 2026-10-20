"""Public task semantics and evidence contracts for the opt-in Stage 7 research path."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AnswerSpec(Contract):
    columns: list[str] = Field(default_factory=list, max_length=50)
    shape: Literal["scalar", "rows", "set"] = "rows"
    unit: str | None = None
    decimals: int | None = Field(default=None, ge=0, le=15)
    ties: Literal["all", "single", "unspecified"] = "unspecified"
    empty: Literal["allow", "reject"] = "allow"
    nullable: bool = True
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("duplicate output columns")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("inverted answer range")
        return self


class AnalysisTaskSpec(Contract):
    question: str = Field(min_length=1, max_length=30000)
    public_requirements: str = Field(default="", max_length=30000)
    entity: str | None = None
    grain: list[str] = Field(default_factory=list, max_length=20)
    filters: list[str] = Field(default_factory=list, max_length=50)
    time_range: str | None = None
    formula: str | None = None
    denominator: str | None = None
    count_basis: Literal["entities", "records", "unspecified"] = "unspecified"
    order_by: str | None = None
    order_direction: Literal["asc", "desc"] | None = None
    conflicts: list[str] = Field(default_factory=list, max_length=20)
    answer: AnswerSpec = Field(default_factory=AnswerSpec)


class AnalysisArguments(Contract):
    table: str = Field(min_length=1, max_length=200)
    columns: list[str] = Field(min_length=1, max_length=2)
    operation: Literal["describe", "pearson", "linear_regression"]

    @model_validator(mode="after")
    def paired(self):
        if self.operation != "describe" and len(self.columns) != 2:
            raise ValueError("paired analysis requires two columns")
        return self


class CollectionArguments(Contract):
    sql: str = Field(min_length=1, max_length=30000)


class CrosscheckArguments(CollectionArguments):
    purpose: Literal["ties", "entity_grain", "denominator", "date_boundary", "output_range"]


class ToolAction(Contract):
    sql: str | None = Field(default=None, min_length=1, max_length=30000)
    final_sql: str | None = Field(default=None, min_length=1, max_length=30000)
    search: str | None = Field(default=None, min_length=1, max_length=300)
    analysis: AnalysisArguments | None = None
    collect: CollectionArguments | None = None
    crosscheck: CrosscheckArguments | None = None
    answers: dict[str, Any] | None = None
    evidence_steps: list[int] = Field(default_factory=list, max_length=100)
    # Each answer key binds to a successful step and a path within its full result.
    bindings: dict[str, tuple[int, list[str | int]]] = Field(default_factory=dict)
    contract: AnalysisTaskSpec | None = None
    unsupported: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def one_action(self):
        names = (
            "sql",
            "final_sql",
            "search",
            "analysis",
            "collect",
            "crosscheck",
            "answers",
            "contract",
            "unsupported",
        )
        if sum(getattr(self, name) is not None for name in names) != 1:
            raise ValueError("exactly one typed action required")
        if self.answers is None and (self.evidence_steps or self.bindings):
            raise ValueError("evidence bindings belong to answers only")
        return self
