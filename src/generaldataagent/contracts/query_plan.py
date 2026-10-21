"""A small relational plan language for counts and ranked entities, without arbitrary SQL."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FieldRef(PlanNode):
    relation: str = Field(min_length=1, max_length=200)
    column: str = Field(min_length=1, max_length=200)


class Projection(PlanNode):
    field: FieldRef
    name: str = Field(min_length=1, max_length=200)


class PlanJoin(PlanNode):
    left: FieldRef
    right: FieldRef
    kind: Literal["inner", "left"] = "inner"


class PlanFilter(PlanNode):
    field: FieldRef
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "is_null", "is_not_null"]
    value: Any = None
    phase: Literal["before", "after_rank"] = "before"
    source_excerpt: str = Field(min_length=1, max_length=2000)


class TieBreak(PlanNode):
    field: FieldRef
    direction: Literal["asc", "desc"]
    source_excerpt: str = Field(min_length=1, max_length=2000)


class SemanticQueryPlan(PlanNode):
    operation: Literal["rank", "count", "population_ratio"]
    relations: list[str] = Field(min_length=1, max_length=4)
    joins: list[PlanJoin] = Field(default_factory=list, max_length=3)
    filters: list[PlanFilter] = Field(default_factory=list, max_length=20)
    numerator_filters: list[PlanFilter] = Field(default_factory=list, max_length=20)
    multiplier: Literal[1, 100] = 100
    ratio_name: str = Field(default="ratio", min_length=1, max_length=200)
    entity_keys: list[FieldRef] = Field(default_factory=list, max_length=5)
    projections: list[Projection] = Field(default_factory=list, max_length=20)
    partition_by: list[FieldRef] = Field(default_factory=list, max_length=5)
    metric: FieldRef | None = None
    aggregation: Literal["none", "sum", "avg", "count"] = "none"
    direction: Literal["asc", "desc"] = "desc"
    top_k: int = Field(default=1, ge=1, le=1000)
    ties: Literal["all", "single"] = "all"
    tie_break_by: list[TieBreak] = Field(default_factory=list, max_length=5)
    count_basis: Literal["entities", "records"] = "entities"
    count_name: str = Field(default="count", min_length=1, max_length=200)
    source_excerpt: str = Field(min_length=1, max_length=30000)

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.relations)) != len(self.relations):
            raise ValueError("self-joins require the general SQL path")
        if self.operation == "rank" and (not self.projections or not self.entity_keys):
            raise ValueError("rank plans require explicit output and entity keys")
        if self.operation == "rank" and self.aggregation != "count" and self.metric is None:
            raise ValueError("rank metric is required")
        if (
            self.operation in {"count", "population_ratio"}
            and self.count_basis == "entities"
            and not self.entity_keys
        ):
            raise ValueError("entity counts need explicit entity keys")
        if self.operation != "population_ratio" and self.numerator_filters:
            raise ValueError("numerator filters require a population ratio")
        if self.operation == "population_ratio" and (
            not self.numerator_filters
            or self.projections
            or self.metric
            or self.partition_by
            or self.aggregation not in {"none", "count"}
            or self.tie_break_by
            or self.top_k != 1
            or any(p.phase != "before" for p in self.filters + self.numerator_filters)
        ):
            raise ValueError("population ratios only support two filtered count populations")
        if self.ties == "single" and not self.tie_break_by:
            raise ValueError("single-winner plans require explicit tie ordering")
        if self.ties == "all" and self.tie_break_by:
            raise ValueError("tie ordering cannot discard requested ties")
        if len({p.name.casefold() for p in self.projections}) != len(self.projections):
            raise ValueError("duplicate output aliases")
        return self
