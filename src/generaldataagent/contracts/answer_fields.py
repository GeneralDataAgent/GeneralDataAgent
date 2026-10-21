"""Per-field answer presentation and method requirements."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnswerFieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["number", "integer", "string", "boolean", "set", "list"]
    item_kind: Literal["number", "integer", "string", "boolean"] | None = None
    order: Literal["asc", "desc"] | None = None
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    choices: list[str] | None = Field(default=None, min_length=1, max_length=32)
    nullable: bool | None = None
    decimals: int | None = Field(default=None, ge=0, le=15)
    unit: str | None = Field(default=None, max_length=50)
    method: (
        Literal[
            "sql",
            "describe",
            "describe.sample_std",
            "pearson",
            "pearson_test",
            "independent_t_test",
            "linear_regression",
            "complete_collection",
        ]
        | None
    ) = None
    source_excerpt: str = Field(default="", max_length=2000)
    collection_separator: Literal[",", ", "] | None = None

    @model_validator(mode="after")
    def collection_presentation(self):
        if self.minimum is not None or self.maximum is not None:
            if self.kind not in {"number", "integer"}:
                raise ValueError("numeric bounds belong to numeric fields only")
            if (
                self.minimum is not None
                and self.maximum is not None
                and self.minimum > self.maximum
            ):
                raise ValueError("inverted field bounds")
        if self.choices is not None and (
            self.kind != "string" or len(set(self.choices)) != len(self.choices)
        ):
            raise ValueError("distinct choices belong to string fields only")
        if self.collection_separator is not None and self.kind not in {"set", "list"}:
            raise ValueError("collection separator belongs to collection fields only")
        if (self.item_kind is not None or self.order is not None) and self.kind not in {
            "set",
            "list",
        }:
            raise ValueError("collection element rules belong to collection fields only")
        return self
