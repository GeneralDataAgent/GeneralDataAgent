"""Trusted semantic definitions used to compile safe metric queries."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    description: str = Field(min_length=1, max_length=1_000)
    aliases: tuple[str, ...] = ()
    data_domain: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    relation: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    expression: str = Field(min_length=1, max_length=1_000)
    time_column: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    allowed_dimensions: tuple[str, ...] = ()
    unit: str = Field(default="count", min_length=1, max_length=64)
    owner: str = Field(min_length=1, max_length=128)

    @field_validator("allowed_dimensions")
    @classmethod
    def validate_dimensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        invalid = [value for value in values if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)]
        if invalid:
            raise ValueError(f"invalid dimension identifiers: {invalid}")
        if len(values) != len(set(values)):
            raise ValueError("allowed dimensions must be unique")
        return values

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("metric aliases must contain 1 to 128 characters")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("metric aliases must be unique within a definition")
        return normalized

    def qualified_name(self) -> str:
        return f"{self.name}@{self.version}"
