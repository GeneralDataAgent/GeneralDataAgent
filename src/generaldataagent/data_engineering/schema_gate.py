"""Fail-closed compatibility gate for schema drift."""

from generaldataagent.contracts.data_engineering import RelationSchema, SchemaDiff
from generaldataagent.data_engineering.schema_diff import compare_schemas


class SchemaDriftError(RuntimeError):
    """Carries the exact breaking diff so the orchestrator can replan."""

    def __init__(self, diff: SchemaDiff) -> None:
        self.diff = diff
        changes = ", ".join(f"{item.name}:{item.change_type}" for item in diff.changes)
        super().__init__(f"breaking schema drift requires replanning: {changes}")


def require_compatible_schema(
    expected: RelationSchema,
    actual: RelationSchema,
) -> SchemaDiff:
    """Return additive drift, but stop execution on every breaking change."""
    diff = compare_schemas(expected, actual)
    if diff.breaking:
        raise SchemaDriftError(diff)
    return diff
