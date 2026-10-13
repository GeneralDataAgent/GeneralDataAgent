"""Trusted semantic definitions used to compile safe metric queries."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    description: str = Field(min_length=1, max_length=1_000)
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
        import re

        invalid = [value for value in values if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)]
        if invalid:
            raise ValueError(f"invalid dimension identifiers: {invalid}")
        if len(values) != len(set(values)):
            raise ValueError("allowed dimensions must be unique")
        return values

    def qualified_name(self) -> str:
        return f"{self.name}@{self.version}"
