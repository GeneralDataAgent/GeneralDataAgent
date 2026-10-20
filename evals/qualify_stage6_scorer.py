"""Run concrete SQL counterfactuals. Fixtures are synthetic, not official benchmark scores."""

import json
import sqlite3
import tempfile
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest
from generaldataagent.evaluation.stage_six_metamorphic import (
    OracleSnapshot,
    evaluate_snapshot_family,
)


def main():
    root = Path(__file__).resolve().parents[1]
    cases = [
        {
            "name": "unseen_currency",
            "source": "BIRD Mini-Dev 1471 schema-reduced fixture",
            "schema": "CREATE TABLE customers(id INTEGER PRIMARY KEY,Currency TEXT)",
            "base": "INSERT INTO customers VALUES (1,'EUR'),(2,'EUR'),(3,'CZK')",
            "mutation": "INSERT INTO customers VALUES (4,'USD')",
            "gold": (
                "SELECT 1.0*SUM(CASE WHEN Currency='EUR' THEN 1 ELSE 0 END)/"
                "SUM(CASE WHEN Currency='CZK' THEN 1 ELSE 0 END) FROM customers"
            ),
            "bad": (
                "SELECT 1.0*SUM(CASE WHEN Currency='EUR' THEN 1 ELSE 0 END)/"
                "(COUNT(*)-SUM(CASE WHEN Currency='EUR' THEN 1 ELSE 0 END)) FROM customers"
            ),
        },
        {
            "name": "duplicate_values",
            "source": "synthetic enterprise aggregation fixture",
            "schema": "CREATE TABLE events(id INTEGER PRIMARY KEY,value REAL)",
            "base": "INSERT INTO events VALUES (1,10),(2,20)",
            "mutation": "INSERT INTO events VALUES (3,10)",
            "gold": "SELECT COUNT(value) FROM events",
            "bad": "SELECT COUNT(DISTINCT value) FROM events",
        },
        {
            "name": "null_values",
            "source": "synthetic enterprise NULL fixture",
            "schema": "CREATE TABLE events(id INTEGER PRIMARY KEY,value REAL)",
            "base": "INSERT INTO events VALUES (1,10),(2,20)",
            "mutation": "INSERT INTO events VALUES (3,NULL)",
            "gold": "SELECT COUNT(value) FROM events",
            "bad": "SELECT COUNT(*) FROM events",
        },
        {
            "name": "exclusive_end",
            "source": "synthetic enterprise date-boundary fixture",
            "schema": "CREATE TABLE events(id INTEGER PRIMARY KEY,day TEXT)",
            "base": "INSERT INTO events VALUES (1,'2026-01-01'),(2,'2026-01-15')",
            "mutation": "INSERT INTO events VALUES (3,'2026-02-01')",
            "gold": "SELECT COUNT(*) FROM events WHERE day>='2026-01-01' AND day<'2026-02-01'",
            "bad": "SELECT COUNT(*) FROM events WHERE day BETWEEN '2026-01-01' AND '2026-02-01'",
        },
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="stage6-counterfactual-") as directory:
        for i, case in enumerate(cases):
            snapshots = []
            for kind in ["base", "mutation"]:
                path = Path(directory) / f"{i}-{kind}.sqlite"
                with sqlite3.connect(path) as db:
                    db.execute(case["schema"])
                    db.execute(case["base"])
                    if kind == "mutation":
                        db.execute(case["mutation"])
                snapshots.append(OracleSnapshot(kind, path, digest(path)))
            mutant = evaluate_snapshot_family(
                reference_sql=case["gold"], candidate_sql=case["bad"], snapshots=snapshots
            )
            control = evaluate_snapshot_family(
                reference_sql=case["gold"], candidate_sql=case["gold"], snapshots=snapshots
            )
            results.append(
                {
                    "name": case["name"],
                    "source": case["source"],
                    "mutant": mutant,
                    "control": control,
                    "qualified": mutant["baseline_match"]
                    and not mutant["all_snapshots_match"]
                    and control["all_snapshots_match"],
                }
            )
    report = {
        "kind": "metamorphic_scorer_qualification",
        "n": len(results),
        "qualified": all(x["qualified"] for x in results),
        "results": results,
        "agent_accuracy": None,
        "production_accepted": False,
    }
    output = root / ".artifacts/stage6/scorer-counterfactuals.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"qualified": report["qualified"], "families": len(results)}))
    raise SystemExit(0 if report["qualified"] else 1)


if __name__ == "__main__":
    main()
