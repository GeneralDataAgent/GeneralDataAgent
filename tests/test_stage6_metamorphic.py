"""Executable SQL fixtures catch plausible queries that agree by accident on one snapshot."""

import sqlite3

import pytest

from generaldataagent.evaluation.stage_six import digest
from generaldataagent.evaluation.stage_six_metamorphic import (
    OracleSnapshot,
    evaluate_snapshot_family,
)


def test_duplicate_counterfactual_falsifies_distinct_count(tmp_path):
    snapshots = []
    for index, rows in enumerate([[(1,), (2,)], [(1,), (2,), (1,)]]):
        path = tmp_path / f"{index}.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE values_table(value INTEGER)")
            db.executemany("INSERT INTO values_table VALUES (?)", rows)
        snapshots.append(OracleSnapshot(str(index), path, digest(path)))
    result = evaluate_snapshot_family(
        reference_sql="SELECT COUNT(value) FROM values_table",
        candidate_sql="SELECT COUNT(DISTINCT value) FROM values_table",
        snapshots=snapshots,
    )
    assert result["baseline_match"] and not result["all_snapshots_match"]
    with pytest.raises(ValueError):
        evaluate_snapshot_family(
            reference_sql="SELECT 1", candidate_sql="SELECT 1", snapshots=snapshots + snapshots
        )
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        evaluate_snapshot_family(
            reference_sql="SELECT 1", candidate_sql="SELECT 1", snapshots=snapshots
        )
