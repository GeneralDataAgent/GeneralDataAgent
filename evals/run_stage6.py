"""Run public development cases through a bounded real-model read-only SQL baseline."""

import argparse
import hashlib
import inspect
import json
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.evaluation.stage_six import (
    SYSTEM_PROMPT,
    add_json_tables,
    build_csv_database,
    digest,
    equivalent_rows,
    load_bird,
    load_dabstep,
    load_infi,
    run_isolated,
    sample_cases,
    score_answers,
    summarize,
    verify_sources,
)
from generaldataagent.evaluation.stage_six_control import (
    ServiceCircuitBreaker,
    not_attempted_result,
)
from generaldataagent.evaluation.stage_six_import import (
    build_identifier_preserving_database,
)
from generaldataagent.evaluation.stage_six_models import resolve_evaluation_model
from generaldataagent.evaluation.stage_six_protocol import evaluation_protocol
from generaldataagent.tools import SQLiteReadOnlyExecutor

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=["infi", "dabstep", "bird"], required=True)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--app-type", choices=["claude", "codex"], default="claude")
    parser.add_argument("--provider-id")
    parser.add_argument(
        "--csv-importer", choices=["legacy-v1", "identifier-v2"], default="legacy-v1"
    )
    parser.add_argument("--request-interval-seconds", type=float, default=0)
    parser.add_argument("--thinking", choices=["default", "on", "off"], default="default")
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument(
        "--tool-profile",
        choices=["sql", "sql-analysis", "sql-repair", "sql-precision"],
        default="sql",
    )
    parser.add_argument("--model", help="Per-run model ID; never modifies CC Switch")
    parser.add_argument("--transport", choices=["configured", "openai-chat"], default="configured")
    parser.add_argument("--bird-databases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.tool_profile == "sql-precision" and args.benchmark != "bird":
        parser.error("SQL precision requires the SQL-output benchmark")
    if not 0 <= args.request_interval_seconds <= 10:
        parser.error("request interval must be finite and between zero and ten seconds")
    if args.output.exists():
        parser.error("output exists; choose a new run directory")
    root = ROOT / "data/stage6"
    manifest = verify_sources(root)
    cases = (
        load_infi(root / "infi")
        if args.benchmark == "infi"
        else load_dabstep(root / "dabstep")
        if args.benchmark == "dabstep"
        else load_bird(root / "bird/mini_dev_sqlite.json")
    )
    selected = sample_cases(cases, args.limit, args.seed)
    if args.benchmark == "bird" and not args.bird_databases:
        parser.error("BIRD real database snapshot required; gold SQL is never a substitute")
    if args.benchmark == "bird":
        database_manifest = json.loads((root / "bird/database-manifest.json").read_text())
        expected_hashes = {
            Path(item["path"]).parent.name: item["sha256"]
            for item in database_manifest["databases"]
        }
        for asset in {case.task.asset for case in selected}:
            path = args.bird_databases / asset / (asset + ".sqlite")
            if digest(path) != expected_hashes[asset]:
                raise ValueError("BIRD database does not match the pinned official snapshot")
    provider, route = resolve_evaluation_model(
        app_type=args.app_type,
        provider_id=args.provider_id,
        model_override=args.model,
        transport=args.transport,
        thinking_mode=args.thinking,
        json_mode=args.json_mode,
    )
    build_csv = (
        build_identifier_preserving_database
        if args.csv_importer == "identifier-v2"
        else build_csv_database
    )
    args.output.mkdir(parents=True)
    code_hash = digest(Path(__file__).resolve())
    protocol = evaluation_protocol(workers=args.workers)
    protocol["request_interval_seconds"] = args.request_interval_seconds
    if args.csv_importer == "identifier-v2":
        protocol["csv_importer"] = args.csv_importer
        protocol["numeric_import"] = (
            "preserve leading zero/large integer text; other finite numeric REAL"
        )
        protocol["import_source_hash"] = hashlib.sha256(
            (
                (ROOT / "src/generaldataagent/evaluation/stage_six_import.py").read_text()
                + inspect.getsource(add_json_tables)
            ).encode()
        ).hexdigest()
    system_fingerprint = {
        "prompt_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "agent_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six.py"),
        "analysis_tools_hash": digest(
            ROOT / "src/generaldataagent/evaluation/stage_six_analysis.py"
        ),
        "precision_tools_hash": digest(
            ROOT / "src/generaldataagent/evaluation/stage_six_sql_precision.py"
        ),
        "repair_tools_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six_repair.py"),
        "model_adapter_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six_models.py"),
        "worker_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six_worker.py"),
        "dependency_lock_hash": digest(ROOT / "uv.lock"),
        "tool_profile": args.tool_profile,
        "route_hash": route["route_hash"],
    }
    report = {
        "benchmark": selected[0].task.benchmark,
        "started_at": datetime.now(UTC).isoformat(),
        "population_n": len(cases),
        "seed": args.seed,
        "requested_n": args.limit,
        "selection": "difficulty x asset round-robin",
        "cohort_hash": hashlib.sha256(
            json.dumps([asdict(c.task) for c in selected], sort_keys=True).encode()
        ).hexdigest(),
        "protocol_hash": hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest(),
        "protocol": protocol,
        "system_fingerprint": system_fingerprint,
        "source_manifest_hash": digest(root / "sources.json"),
        "source_revisions": sorted({x["revision"] for x in manifest["sources"]}),
        "runner_hash": code_hash,
        "python": platform.python_version(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "model": provider.model,
        "model_route": route,
        "tool_profile": args.tool_profile,
        "provider_id": provider.provider_id,
        "results": [],
        "scope": "bounded SQL evaluation baseline; not production Supervisor or Python DS agent",
        "contamination": "public development data; no training-contamination guarantee",
        "production_accepted": False,
    }
    databases = {}
    docs = ""
    if args.benchmark == "dabstep":
        database = (args.output / "databases/payments.sqlite").resolve()
        d = root / "dabstep"
        build_csv(
            {
                name: d / (name + ".csv")
                for name in ["payments", "acquirer_countries", "merchant_category_codes"]
            },
            database,
        )
        add_json_tables(
            {name: d / (name + ".json") for name in ["fees", "merchant_data"]}, database
        )
        docs = "\n".join((d / name).read_text() for name in ["manual.md", "payments-readme.md"])
        databases["payments.csv"] = database
    for case in selected:
        asset = case.task.asset
        if asset not in databases:
            if args.benchmark == "bird":
                database = (args.bird_databases / asset / (asset + ".sqlite")).resolve(strict=True)
                if not database.is_relative_to(args.bird_databases.resolve()):
                    raise ValueError("invalid database path")
            else:
                database = (
                    args.output
                    / "databases"
                    / (hashlib.sha256(asset.encode()).hexdigest() + ".sqlite")
                ).resolve()
                build_csv({"data": root / "infi/da-dev-tables" / asset}, database)
            databases[asset] = database

    circuit = ServiceCircuitBreaker()

    def evaluate(case):
        database = databases[case.task.asset]
        asset = case.task.asset
        before = digest(database)
        if circuit.reason:
            result = not_attempted_result(circuit.reason)
        else:
            result = run_isolated(
                case.task,
                database,
                app_type=args.app_type,
                provider_id=provider.provider_id,
                documents=docs,
                model_override=args.model,
                transport=args.transport,
                route_hash=route["route_hash"],
                tool_profile=args.tool_profile,
                thinking_mode=args.thinking,
                json_mode=args.json_mode,
                request_interval_seconds=args.request_interval_seconds,
                pacing_database=ROOT / ".state/stage6/request-pacing.sqlite",
            )
            result["attempted"] = True
            circuit.observe(result)
        if case.reference_sql:
            expected = None
            try:
                executor = SQLiteReadOnlyExecutor(database, max_rows=10000, timeout_seconds=10)
                expected = executor.execute(case.reference_sql)
            except Exception:
                result["failure"] = "oracle_environment_error"
            passed, hits, total = False, 0, 1
            if expected is not None and result["final_sql"]:
                try:
                    actual = executor.execute(result["final_sql"])
                    passed = equivalent_rows(actual.rows, expected.rows)
                    hits = int(passed)
                except Exception:
                    result["failure"] = "candidate_execution_error"
            elif expected is not None and not result["failure"]:
                result["failure"] = "invalid_output"
        else:
            passed, hits, total = score_answers(case, result["answers"])
        unchanged = before == digest(database)
        result.update(
            case_id=case.task.case_id,
            difficulty=case.task.difficulty,
            asset=asset,
            database_hash=before,
            passed=passed and not result["failure"] and unchanged,
            sub_hits=hits,
            sub_total=total,
            severe_incident=not unchanged,
        )
        if not result["passed"] and not result["failure"]:
            result["failure"] = "answer_mismatch" if unchanged else "database_mutated"
        print(
            json.dumps(
                {
                    "case": case.task.case_id,
                    "passed": result["passed"],
                    "failure": result["failure"],
                    "elapsed": round(result["elapsed_seconds"], 2),
                }
            ),
            flush=True,
        )
        return result

    (args.output / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(evaluate, selected):
            report["results"].append(result)
            (args.output / "report.partial.json").write_text(json.dumps(report, indent=2) + "\n")
    report["summary"] = summarize(report["results"])
    report["service_circuit_reason"] = circuit.reason
    report["run_status"] = "blocked_by_service" if circuit.reason else "completed"
    report["completed_at"] = datetime.now(UTC).isoformat()
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.output / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2), flush=True)
    if circuit.reason:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
