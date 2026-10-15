"""Deterministic schema drift detection."""

from generaldataagent.contracts.data_engineering import (
    ColumnChange,
    RelationSchema,
    SchemaDiff,
)


def compare_schemas(before: RelationSchema, after: RelationSchema) -> SchemaDiff:
    if before.relation != after.relation:
        raise ValueError("schema diff requires the same logical relation")

    old = {column.name.casefold(): column for column in before.columns}
    new = {column.name.casefold(): column for column in after.columns}
    changes: list[ColumnChange] = []

    for key in sorted(old.keys() - new.keys()):
        changes.append(ColumnChange(name=old[key].name, change_type="removed", before=old[key]))
    for key in sorted(new.keys() - old.keys()):
        changes.append(ColumnChange(name=new[key].name, change_type="added", after=new[key]))
    for key in sorted(old.keys() & new.keys()):
        previous = old[key]
        current = new[key]
        if previous.data_type.casefold() != current.data_type.casefold():
            changes.append(
                ColumnChange(
                    name=current.name,
                    change_type="type_changed",
                    before=previous,
                    after=current,
                )
            )
        if previous.nullable != current.nullable:
            changes.append(
                ColumnChange(
                    name=current.name,
                    change_type="nullability_changed",
                    before=previous,
                    after=current,
                )
            )

    return SchemaDiff(
        relation=before.relation,
        changes=tuple(changes),
        breaking=any(
            item.change_type in {"removed", "type_changed", "nullability_changed"}
            for item in changes
        ),
    )
