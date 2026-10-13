"""Safe construction of dry-run-only transformation proposals."""

from collections.abc import Iterable

from generaldataagent.contracts.data_engineering import (
    TransformationProposal,
    TransformationTest,
)
from generaldataagent.tools import SqlGuard


def build_transformation_proposal(
    *,
    connector_id: str,
    dialect: str,
    target_relation: str,
    select_sql: str,
    allowed_relations: set[str],
    tests: Iterable[TransformationTest],
    impact_summary: str,
    rollback_plan: str,
) -> TransformationProposal:
    """Parse and normalize a read-only query without applying the target change."""

    inspection = SqlGuard(dialect=dialect).inspect(
        select_sql,
        allowed_relations=allowed_relations,
    )
    if not inspection.relations:
        raise ValueError("a transformation proposal must read at least one source relation")
    return TransformationProposal(
        connector_id=connector_id,
        target_relation=target_relation,
        dialect=dialect,
        select_sql=inspection.normalized_sql,
        dependencies=tuple(sorted(inspection.relations, key=str.casefold)),
        tests=tuple(tests),
        impact_summary=impact_summary,
        rollback_plan=rollback_plan,
    )
