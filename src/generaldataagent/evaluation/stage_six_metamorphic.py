"""Result equivalence across controlled snapshots catches accidental baseline agreement."""

from dataclasses import dataclass
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest, equivalent_rows
from generaldataagent.tools import SQLiteReadOnlyExecutor
from generaldataagent.tools.sql_guard import SqlGuard


@dataclass(frozen=True)
class OracleSnapshot:
    snapshot_id: str
    path: Path
    sha256: str


def evaluate_snapshot_family(
    *, reference_sql: str, candidate_sql: str, snapshots: list[OracleSnapshot]
) -> dict:
    if not snapshots or len({s.snapshot_id for s in snapshots}) != len(snapshots):
        raise ValueError("empty or duplicate snapshot family")
    guard = SqlGuard(dialect="sqlite")
    guard.inspect(reference_sql)
    guard.inspect(candidate_sql)
    results = []
    for snapshot in snapshots:
        if digest(snapshot.path) != snapshot.sha256:
            raise ValueError("counterfactual snapshot changed")
        executor = SQLiteReadOnlyExecutor(snapshot.path, max_rows=1000, timeout_seconds=5)
        expected = executor.execute(reference_sql)
        actual = executor.execute(candidate_sql)
        results.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "passed": equivalent_rows(actual.rows, expected.rows),
                "snapshot_sha256": snapshot.sha256,
            }
        )
    return {
        "baseline_match": results[0]["passed"],
        "all_snapshots_match": all(r["passed"] for r in results),
        "results": results,
        "agent_accuracy": None,
        "production_accepted": False,
    }
