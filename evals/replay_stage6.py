"""Independently replay stored predictions against pinned labels/databases, without a model."""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from generaldataagent.evaluation.stage_six import (
    digest,
    equivalent_rows,
    load_bird,
    load_dabstep,
    load_infi,
    sample_cases,
    score_answers,
    verify_sources,
)
from generaldataagent.tools import SQLiteReadOnlyExecutor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "data/stage6"
    verify_sources(source)
    report = json.loads(args.report.read_text())
    if digest(source / "sources.json") != report["source_manifest_hash"]:
        raise ValueError("source manifest differs from original run")
    benchmark = report["benchmark"]
    population = (
        load_infi(source / "infi")
        if benchmark == "infiagent-dabench"
        else load_dabstep(source / "dabstep")
        if benchmark == "dabstep-dev"
        else load_bird(source / "bird/mini_dev_sqlite.json")
    )
    cases = sample_cases(population, report["requested_n"], report["seed"])
    cohort = hashlib.sha256(
        json.dumps([asdict(c.task) for c in cases], sort_keys=True).encode()
    ).hexdigest()
    if cohort != report["cohort_hash"] or [c.task.case_id for c in cases] != [
        x["case_id"] for x in report["results"]
    ]:
        raise ValueError("missing, duplicated, reordered or substituted cohort")
    rows = []
    for case, original in zip(cases, report["results"], strict=True):
        if case.reference_sql:
            db = source / "bird/databases" / case.task.asset / (case.task.asset + ".sqlite")
        elif benchmark == "dabstep-dev":
            db = args.report.parent / "databases/payments.sqlite"
        else:
            db = (
                args.report.parent
                / "databases"
                / (hashlib.sha256(case.task.asset.encode()).hexdigest() + ".sqlite")
            )
        if digest(db) != original["database_hash"]:
            raise ValueError("task database changed")
        passed = False
        if case.reference_sql and original["final_sql"]:
            try:
                executor = SQLiteReadOnlyExecutor(db, max_rows=10000, timeout_seconds=10)
                passed = equivalent_rows(
                    executor.execute(original["final_sql"]).rows,
                    executor.execute(case.reference_sql).rows,
                )
            except Exception:
                passed = False
        elif not case.reference_sql:
            passed, _, _ = score_answers(case, original["answers"])
        replayed = bool(passed and not original["failure"] and not original["severe_incident"])
        rows.append(
            {
                "case_id": case.task.case_id,
                "original": original["passed"],
                "replayed": replayed,
                "agrees": original["passed"] == replayed,
            }
        )
    result = {
        "benchmark": benchmark,
        "report_sha256": digest(args.report),
        "n": len(rows),
        "all_agree": all(x["agrees"] for x in rows),
        "results": rows,
        "new_model_calls": 0,
        "production_accepted": False,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}))
    raise SystemExit(0 if result["all_agree"] else 1)


if __name__ == "__main__":
    main()
