"""Execute every official gold SQL to qualify data/environment. This is not an agent score."""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest, load_bird
from generaldataagent.tools import SQLiteReadOnlyExecutor


def main():
    root = Path(__file__).resolve().parents[1]
    data = root / "data/stage6/bird"
    manifest = json.loads((data / "database-manifest.json").read_text())
    for entry in manifest["databases"]:
        if digest(data / entry["path"]) != entry["sha256"]:
            raise ValueError("BIRD database integrity failure")
    cases = load_bird(data / "mini_dev_sqlite.json")

    def execute(case):
        db = data / "databases" / case.task.asset / (case.task.asset + ".sqlite")
        try:
            result = SQLiteReadOnlyExecutor(db, max_rows=10000, timeout_seconds=10).execute(
                case.reference_sql
            )
            return {
                "case_id": case.task.case_id,
                "database": case.task.asset,
                "status": "ok",
                "rows": len(result.rows),
                "elapsed_ms": result.elapsed_ms,
            }
        except Exception as exc:
            return {
                "case_id": case.task.case_id,
                "database": case.task.asset,
                "status": "error",
                "error": type(exc).__name__,
            }

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, cases))
    report = {
        "kind": "reference_SQL_environment_qualification",
        "agent_accuracy": None,
        "question_hash": digest(data / "mini_dev_sqlite.json"),
        "database_manifest_hash": digest(data / "database-manifest.json"),
        "counts": dict(Counter(x["status"] for x in results)),
        "results": results,
    }
    output = root / ".artifacts/stage6/bird-environment.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["counts"]))


if __name__ == "__main__":
    main()
