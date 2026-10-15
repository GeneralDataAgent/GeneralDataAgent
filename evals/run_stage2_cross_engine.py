"""Cross-check deterministic profiling across SQLite and DuckDB."""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import duckdb

from generaldataagent.connectors import DuckDBConnector, SQLiteConnector
from generaldataagent.data_engineering import compare_profiles, profile_relation


def run_cross_engine_evaluation(case_count: int = 20) -> dict[str, Any]:
    if case_count < 1 or case_count > 100:
        raise ValueError("case_count must be between 1 and 100")
    failures: list[dict[str, Any]] = []
    total_checks = 0
    with TemporaryDirectory(prefix="generaldataagent-cross-engine-") as directory:
        root = Path(directory)
        sqlite_path = root / "profiles.sqlite"
        duckdb_path = root / "profiles.duckdb"
        _create_fixtures(sqlite_path, duckdb_path, case_count)
        sqlite_connector = SQLiteConnector(sqlite_path, connector_id="sqlite-evaluation")
        duckdb_connector = DuckDBConnector(duckdb_path, connector_id="duckdb-evaluation")
        for index in range(case_count):
            relation = f"profile_case_{index:02d}"
            try:
                sqlite_profile = profile_relation(sqlite_connector, relation)
                duckdb_profile = profile_relation(duckdb_connector, relation)
                comparison = compare_profiles(
                    duckdb_profile,
                    sqlite_profile,
                    numeric_tolerance=1e-12,
                    threshold=1.0,
                )
                total_checks += comparison.total_checks
                if not comparison.passed:
                    failures.append(
                        {
                            "case_id": relation,
                            "agreement_rate": comparison.agreement_rate,
                            "mismatches": [
                                mismatch.model_dump(mode="json")
                                for mismatch in comparison.mismatches
                            ],
                        }
                    )
            except Exception as exc:
                failures.append(
                    {
                        "case_id": relation,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    passed = case_count - len(failures)
    return {
        "dataset_size": case_count,
        "passed": passed,
        "failed": len(failures),
        "pass_rate": passed / case_count,
        "total_field_checks": total_checks,
        "failures": failures,
    }


def _create_fixtures(sqlite_path: Path, duckdb_path: Path, case_count: int) -> None:
    with (
        sqlite3.connect(sqlite_path) as sqlite_connection,
        duckdb.connect(str(duckdb_path)) as duckdb_connection,
    ):
        for index in range(case_count):
            relation = f"profile_case_{index:02d}"
            sqlite_connection.execute(
                f'CREATE TABLE "{relation}" (entity_id INTEGER, category TEXT, amount REAL)'
            )
            duckdb_connection.execute(
                f'CREATE TABLE "{relation}" (entity_id INTEGER, category VARCHAR, amount DOUBLE)'
            )
            rows = _case_rows(index)
            if rows:
                sqlite_connection.executemany(
                    f'INSERT INTO "{relation}" VALUES (?, ?, ?)',
                    rows,
                )
                duckdb_connection.executemany(
                    f'INSERT INTO "{relation}" VALUES (?, ?, ?)',
                    rows,
                )


def _case_rows(index: int) -> list[tuple[int | None, str | None, float | None]]:
    categories = ("alpha", "beta", "gamma", "delta")
    rows = []
    for row_index in range(index * 3):
        entity_id = None if (row_index + index) % 11 == 0 else row_index % (index % 5 + 1)
        category = (
            None
            if (row_index + index) % 7 == 0
            else categories[(row_index + index) % len(categories)]
        )
        amount = None if (row_index * 2 + index) % 13 == 0 else float((row_index - index) * 2)
        rows.append((entity_id, category, amount))
    return rows


if __name__ == "__main__":
    result = run_cross_engine_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
