"""Deterministic relation profiling over a read-only connector."""

from generaldataagent.connectors.base import (
    ReadOnlyConnector,
    quote_identifier,
)
from generaldataagent.contracts.data_engineering import ColumnProfile, DataProfile

_NUMERIC_TYPE_MARKERS = ("INT", "REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC")


def profile_relation(connector: ReadOnlyConnector, relation: str) -> DataProfile:
    if not connector.capabilities.read_only or not connector.capabilities.profiling:
        raise ValueError("connector must declare read-only profiling support")

    schema = connector.inspect_relation(relation)
    relation_sql = quote_identifier(relation)
    row_count = int(connector.fetch_all(f"SELECT COUNT(*) FROM {relation_sql}")[0][0])
    profiles: list[ColumnProfile] = []
    for column in schema.columns:
        column_sql = quote_identifier(column.name)
        numeric = any(marker in column.data_type.upper() for marker in _NUMERIC_TYPE_MARKERS)
        mean_sql = f", AVG({column_sql})" if numeric else ""
        row = connector.fetch_all(
            f"SELECT COUNT(*) - COUNT({column_sql}), COUNT(DISTINCT {column_sql}), "
            f"MIN({column_sql}), MAX({column_sql}){mean_sql} FROM {relation_sql}"
        )[0]
        profiles.append(
            ColumnProfile(
                name=column.name,
                data_type=column.data_type,
                row_count=row_count,
                null_count=int(row[0]),
                distinct_count=int(row[1]),
                minimum=row[2],
                maximum=row[3],
                mean=float(row[4]) if numeric and row[4] is not None else None,
            )
        )
    return DataProfile(
        connector_id=connector.capabilities.connector_id,
        relation=relation,
        row_count=row_count,
        columns=tuple(profiles),
    )
