"""Replay observed SQL to measure feedback compaction; no labels or new model requests."""

import argparse
import hashlib
import json
from pathlib import Path

from generaldataagent.evaluation.stage_six import database_context, digest
from generaldataagent.evaluation.stage_six_repair import bounded_observation
from generaldataagent.tools import SQLiteReadOnlyExecutor
from generaldataagent.tools.sql_guard import SqlGuard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    rows = []
    for path in args.report:
        report = json.loads(path.read_text())
        if digest(root / "data/stage6/sources.json") != report["source_manifest_hash"]:
            raise ValueError("source manifest changed")
        for case in report["results"]:
            if report["benchmark"] == "bird-mini-dev-sqlite":
                database = (
                    root
                    / "data/stage6/bird/databases"
                    / case["asset"]
                    / (case["asset"] + ".sqlite")
                )
            elif report["benchmark"] == "dabstep-dev":
                database = path.parent / "databases/payments.sqlite"
            else:
                database = (
                    path.parent
                    / "databases"
                    / (hashlib.sha256(case["asset"].encode()).hexdigest() + ".sqlite")
                )
            if digest(database) != case["database_hash"]:
                raise ValueError("database changed")
            _, relations = database_context(database.resolve())
            executor = SQLiteReadOnlyExecutor(database, max_rows=200, timeout_seconds=5)
            guard = SqlGuard(dialect="sqlite")
            seen = set()
            for event in case["trace"]:
                if event.get("tool") != "sql" or event.get("status") not in {"ok", "repeated"}:
                    continue
                sql = event["sql"]
                if sql in seen:
                    continue
                seen.add(sql)
                guard.inspect(sql, allowed_relations=relations)
                try:
                    result = executor.execute(sql)
                except Exception as exc:
                    rows.append({"case_id": case["case_id"], "error": type(exc).__name__})
                    continue
                observation = {"columns": result.columns, "rows": result.rows}
                actual_hash = hashlib.sha256(
                    json.dumps(observation, sort_keys=True, default=str).encode()
                ).hexdigest()
                if actual_hash != event["result_hash"]:
                    raise ValueError("stored tool result does not reproduce")
                compact = bounded_observation(observation)
                rows.append(
                    {
                        "report_sha256": digest(path),
                        "case_id": case["case_id"],
                        "step": event["step"],
                        "result_hash": actual_hash,
                        "before_characters": len(json.dumps(observation, default=str)),
                        "after_characters": len(json.dumps(compact, default=str)),
                        "truncated": compact.get("truncated", False),
                        "within_budget": len(json.dumps(compact, default=str)) <= 6000,
                    }
                )
    valid = [r for r in rows if "error" not in r]
    output = {
        "kind": "observed_feedback_compaction",
        "new_model_calls": 0,
        "agent_accuracy": None,
        "n": len(rows),
        "reproduced": len(valid),
        "all_within_budget": bool(valid) and all(r["within_budget"] for r in valid),
        "before_characters": sum(r["before_characters"] for r in valid),
        "after_characters": sum(r["after_characters"] for r in valid),
        "truncated_observations": sum(r["truncated"] for r in valid),
        "results": rows,
        "production_accepted": False,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({k: v for k, v in output.items() if k != "results"}))
    raise SystemExit(0 if output["all_within_budget"] and len(valid) == len(rows) else 1)


if __name__ == "__main__":
    main()
