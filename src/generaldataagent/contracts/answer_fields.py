"""Per-field answer presentation and method requirements."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnswerFieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["number", "integer", "string", "boolean", "set", "list"]
    item_kind: Literal["number", "integer", "string", "boolean"] | None = None
    order: Literal["asc", "desc"] | None = None
    nullable: bool | None = None
    decimals: int | None = Field(default=None, ge=0, le=15)
    unit: str | None = Field(default=None, max_length=50)
    method: (
        Literal[
            "sql",
            "describe",
            "describe.sample_std",
            "pearson",
            "linear_regression",
            "complete_collection",
        ]
        | None
    ) = None
    source_excerpt: str = Field(default="", max_length=2000)
    collection_separator: Literal[",", ", "] | None = None

    @model_validator(mode="after")
    def collection_presentation(self):
        if self.collection_separator is not None and self.kind not in {"set", "list"}:
            raise ValueError("collection separator belongs to set fields only")
        if (self.item_kind is not None or self.order is not None) and self.kind not in {
            "set",
            "list",
        }:
            raise ValueError("collection element rules belong to collection fields only")
        return self
