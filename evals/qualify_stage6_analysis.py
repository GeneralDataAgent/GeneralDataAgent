"""Cross-check fixed statistics and CSV ingestion against DuckDB on official source files.

This qualifies tools/data only; it is not an agent benchmark score.
"""

import argparse
import json
import math
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

import duckdb

from generaldataagent.evaluation.stage_six import (
    build_csv_database,
    digest,
    load_infi,
    quote_identifier,
    verify_sources,
)
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis
from generaldataagent.evaluation.stage_six_import import build_identifier_preserving_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv-importer", choices=["legacy-v1", "identifier-v2"], default="legacy-v1"
    )
    parser.add_argument("--include-dabstep", action="store_true")
    args = parser.parse_args()
    build_csv = (
        build_identifier_preserving_database
        if args.csv_importer == "identifier-v2"
        else build_csv_database
    )
    root = Path(__file__).resolve().parents[1]
    source = root / "data/stage6"
    verify_sources(source)
    files = [
        (name, source / "infi/da-dev-tables" / name)
        for name in sorted({case.task.asset for case in load_infi(source / "infi")})
    ]
    if args.include_dabstep:
        files.extend(
            ("dabstep/" + name, source / "dabstep" / name)
            for name in ["payments.csv", "acquirer_countries.csv", "merchant_category_codes.csv"]
        )
    records = []
    with tempfile.TemporaryDirectory(prefix="stage6-qualification-") as temporary:
        for index, (filename, path) in enumerate(files):
            database = Path(temporary) / (str(index) + ".sqlite")
            build_csv({"data": path}, database)
            with sqlite3.connect(database) as sqlite_db:
                columns = sqlite_db.execute("PRAGMA table_info(data)").fetchall()
                count = sqlite_db.execute("SELECT COUNT(*) FROM data").fetchone()[0]
            with duckdb.connect() as oracle:
                oracle.execute(
                    "CREATE TABLE raw AS SELECT * FROM read_csv(?,header=true,all_varchar=true)",
                    [str(path)],
                )
                duck_columns = [
                    item[0] for item in oracle.execute("SELECT * FROM raw LIMIT 0").description
                ]
                expected_count = oracle.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
                record = {
                    "asset": filename,
                    "source_sha256": digest(path),
                    "rows": count,
                    "row_count_matches": count == expected_count,
                    "checks": [],
                    "semantic_warnings": [],
                    "identifier_checks": [],
                }
                selected = [col for col in columns if col[2] == "REAL"][:3]
                for column in selected:
                    name = column[1]
                    raw_column = quote_identifier(duck_columns[column[0]])
                    cleaned = f"NULLIF(trim(replace({raw_column}, ',', '')), '')"
                    numeric = f"CAST({cleaned} AS DOUBLE)"
                    expected = oracle.execute(
                        f"SELECT count({numeric}),sum({numeric}),avg({numeric}),"
                        f"median({numeric}),stddev_samp({numeric}),"
                        f"min({numeric}),max({numeric}) FROM raw"
                    ).fetchone()
                    if expected[0] == 0:
                        try:
                            fixed_analysis(
                                database,
                                {"table": "data", "columns": [name], "operation": "describe"},
                                allowed_relations={"data"},
                            )
                            rejected = False
                        except ValueError as exc:
                            rejected = str(exc) == "no finite observations"
                        record["checks"].append(
                            {
                                "column": name,
                                "kind": "empty_column_rejection",
                                "passed": rejected,
                                "assertions": 1,
                            }
                        )
                    else:
                        actual = fixed_analysis(
                            database,
                            {"table": "data", "columns": [name], "operation": "describe"},
                            allowed_relations={"data"},
                        )["statistics"][name]
                        observed = [
                            actual[k]
                            for k in ["count", "sum", "mean", "median", "sample_std", "min", "max"]
                        ]
                        equal = all(
                            a is b
                            if a is None or b is None
                            else math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)
                            for a, b in zip(observed, expected, strict=True)
                        )
                        record["checks"].append(
                            {
                                "column": name,
                                "kind": "descriptive_statistics",
                                "passed": equal,
                                "assertions": 7,
                            }
                        )
                for column in columns:
                    raw_column = quote_identifier(duck_columns[column[0]])
                    leading = oracle.execute(
                        f"SELECT count(*) FROM raw WHERE "
                        f"regexp_matches(trim({raw_column}), '^[+-]?0[0-9]+$')"
                    ).fetchone()[0]
                    large = oracle.execute(
                        f"SELECT count(*) FROM raw WHERE "
                        f"regexp_matches(trim({raw_column}), '^[+-]?[0-9]+$') "
                        f"AND abs(try_cast({raw_column} AS HUGEINT))>9007199254740992"
                    ).fetchone()[0]
                    if leading or large:
                        raw_values = oracle.execute("SELECT " + raw_column + " FROM raw").fetchall()
                        expected_values = [
                            None if value is None or not value.strip() else value
                            for (value,) in raw_values
                        ]
                        with sqlite3.connect(database) as imported:
                            actual_values = [
                                value
                                for (value,) in imported.execute(
                                    "SELECT " + quote_identifier(column[1]) + " FROM data"
                                )
                            ]
                        preserved = Counter(actual_values) == Counter(expected_values)
                        record["identifier_checks"].append(
                            {
                                "column": column[1],
                                "passed": preserved,
                                "leading_zero_values": leading,
                                "large_integer_values": large,
                            }
                        )
                    if (leading or large) and column[2] == "REAL":
                        record["semantic_warnings"].append(
                            {
                                "column": column[1],
                                "leading_zero_values": leading,
                                "integers_above_exact_double_range": large,
                            }
                        )
                records.append(record)
            print(
                json.dumps(
                    {
                        "asset": filename,
                        "checks": len(record["checks"]),
                        "warnings": len(record["semantic_warnings"]),
                    }
                ),
                flush=True,
            )
    report = {
        "kind": "cross_engine_tool_and_import_qualification",
        "csv_importer": args.csv_importer,
        "agent_accuracy": None,
        "files": len(records),
        "row_count_matches": sum(r["row_count_matches"] for r in records),
        "identifier_check_counts": dict(
            Counter(
                "passed" if item["passed"] else "failed"
                for record in records
                for item in record["identifier_checks"]
            )
        ),
        "check_counts": dict(
            Counter("passed" if c["passed"] else "failed" for r in records for c in r["checks"])
        ),
        "files_with_semantic_warnings": sum(bool(r["semantic_warnings"]) for r in records),
        "scope": (
            "52 referenced InfiAgent CSVs plus optional 3 DABstep CSVs; "
            "first 3 numeric columns and identifier values"
        ),
        "includes_dabstep": args.include_dabstep,
        "source_manifest_hash": digest(source / "sources.json"),
        "records": records,
    }
    suffix = "-id-audit" if args.csv_importer == "legacy-v1" else "-identifier-v2-id-audit"
    if args.include_dabstep:
        suffix += "-with-dabstep"
    output = root / (".artifacts/stage6/analysis-cross-engine" + suffix + ".json")
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))


if __name__ == "__main__":
    main()
