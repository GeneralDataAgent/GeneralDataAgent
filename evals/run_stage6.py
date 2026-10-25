"""Run public development cases through a bounded real-model read-only SQL baseline."""

import argparse
import hashlib
import inspect
import json
import platform
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.routing import route_task
from generaldataagent.evaluation.stage_six import (
    DEADLINE_MARGIN_SECONDS,
    DEFAULT_WORKER_REPAIRS,
    DEFAULT_WORKER_SECONDS,
    DEFAULT_WORKER_STEPS,
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
    service_readiness,
)
from generaldataagent.evaluation.stage_six_import import (
    build_identifier_preserving_database,
)
from generaldataagent.evaluation.stage_six_models import resolve_evaluation_model
from generaldataagent.evaluation.stage_six_protocol import evaluation_protocol
from generaldataagent.tools import SQLiteReadOnlyExecutor

ROOT = Path(__file__).resolve().parents[1]
# Prepared public snapshots are local inputs, never committed. Keeping the
# location as one module constant lets an orchestration test supply synthetic
# cases and documents without a prepared snapshot present.
DATA_ROOT = ROOT / "data/stage6"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=["infi", "dabstep", "bird"], required=True)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument(
        "--evaluation-protocol",
        choices=["stage6-v7", "stage7-v1", "stage8-v1", "stage8-v2", "stage9-v1"],
        default="stage6-v7",
    )
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--app-type", choices=["claude", "codex", "magpie"], default="claude")
    parser.add_argument("--provider-id")
    parser.add_argument(
        "--csv-importer", choices=["legacy-v1", "identifier-v2"], default="legacy-v1"
    )
    parser.add_argument("--request-interval-seconds", type=float, default=0)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Optional total observed token cap; default: no evaluator token cap",
    )
    parser.add_argument("--preflight-report", type=Path)
    parser.add_argument("--thinking", choices=["default", "on", "off"], default="default")
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument(
        "--tool-profile",
        choices=[
            "sql",
            "sql-analysis",
            "sql-repair",
            "sql-precision",
            "sql-semantic",
            "sql-semantic-plan",
            "sql-semantic-json",
            "sql-science",
            "sql-rules",
        ],
        default="sql",
    )
    parser.add_argument(
        "--routing",
        action="store_true",
        help=(
            "Choose the lane per case from public question text. Off by default so "
            "existing paired baselines keep a single fixed profile."
        ),
    )
    parser.add_argument(
        "--experience-database",
        type=Path,
        help=(
            "Reviewed experience store. Off by default; only active records are "
            "retrieved, and the hints never contain questions, answers or references."
        ),
    )
    parser.add_argument(
        "--experience-actor",
        help="Actor scope for experience retrieval, as JSON with user_id/tenant_id/"
        "project_id/data_domains. Required with --experience-database.",
    )
    parser.add_argument(
        "--model",
        help="Per-run model ID (required for Magpie); never modifies desktop configuration",
    )
    parser.add_argument("--transport", choices=["configured", "openai-chat"], default="configured")
    parser.add_argument("--bird-databases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_tokens is not None and args.max_tokens < 1:
        parser.error("max tokens must be positive")
    if args.tool_profile in {
        "sql-semantic",
        "sql-semantic-plan",
        "sql-semantic-json",
    } and args.evaluation_protocol not in {"stage7-v1", "stage8-v1", "stage8-v2"}:
        parser.error("sql-semantic requires stage7-v1; rerun both comparison arms")
    if args.tool_profile == "sql-rules" and (
        args.evaluation_protocol != "stage9-v1" or args.benchmark != "dabstep"
    ):
        parser.error("sql-rules requires stage9-v1 and dabstep")
    if args.tool_profile == "sql-science" and args.evaluation_protocol != "stage8-v2":
        parser.error("sql-science requires stage8-v2; rerun both comparison arms")
    if args.tool_profile == "sql-semantic-plan" and args.benchmark != "bird":
        parser.error("structured query plans require the SQL-output benchmark")
    if args.tool_profile == "sql-semantic-json" and args.benchmark != "bird":
        parser.error("JSON SQL mode requires the SQL-output benchmark")
    if args.tool_profile == "sql-precision" and args.benchmark != "bird":
        parser.error("SQL precision requires the SQL-output benchmark")
    if not 0 <= args.request_interval_seconds <= 60:
        parser.error("request interval must be finite and between zero and sixty seconds")
    if args.output.exists():
        parser.error("output exists; choose a new run directory")
    if args.routing and args.tool_profile != "sql":
        parser.error("--routing selects the profile per case; drop --tool-profile")
    if args.routing and args.benchmark != "dabstep":
        parser.error("routing lanes are only qualified for the fee-rule benchmark")
    if (args.experience_database is None) != (args.experience_actor is None):
        parser.error("experience reuse requires both --experience-database and --experience-actor")
    if args.experience_database is not None:
        if not args.experience_database.is_file():
            parser.error("experience database does not exist")
        try:
            actor = ActorContext.model_validate(json.loads(args.experience_actor))
            experience_actor = actor.model_dump(mode="json")
            from generaldataagent.business.actions import content_hash
            from generaldataagent.business.governance import actor_scope

            experience_scope_hash = content_hash(json.loads(actor_scope(actor)))
        except (json.JSONDecodeError, ValueError) as exc:
            parser.error(f"invalid experience actor scope: {exc}")
        # A misconfigured store would otherwise fail every case as a worker
        # error while still spending a full run. Require the store shape and
        # internal consistency up front; the audit is read-only.
        from generaldataagent.business.experience import ExperienceError, ExperienceStore

        try:
            experience_audit = ExperienceStore(
                args.experience_database, clock=lambda: None, read_only=True
            ).audit()
        except sqlite3.OperationalError as exc:
            # A readable SQLite file without the experience schema.
            parser.error(f"experience database is not an experience store: {exc}")
        except sqlite3.DatabaseError as exc:
            # An unreadable file is an environment failure, not a bad argument.
            raise exc
        except (ExperienceError, FileNotFoundError) as exc:
            parser.error(f"experience database is not a usable experience store: {exc}")
        if not experience_audit["consistent"]:
            parser.error(
                "experience database failed its consistency audit: "
                f"{len(experience_audit['findings'])} finding(s)"
            )
    else:
        experience_actor = None
        experience_scope_hash = None
        experience_audit = None
    root = DATA_ROOT
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
        max_output_tokens=args.max_output_tokens,
    )
    build_csv = (
        build_identifier_preserving_database
        if args.csv_importer == "identifier-v2"
        else build_csv_database
    )
    args.output.mkdir(parents=True)
    code_hash = digest(Path(__file__).resolve())
    # The protocol must state the deadline that will actually apply. Routed
    # runs pick the lane per case, so the reported ceiling is the largest lane
    # budget plus the same margin the worker process gets.
    lane_budgets = {
        lane: route_task("x", override=lane).max_seconds
        for lane in ("short", "analysis", "governed")
    }
    lane_steps = {
        lane: route_task("x", override=lane).max_steps for lane in ("short", "analysis", "governed")
    }
    lane_repairs = {
        lane: route_task("x", override=lane).max_repairs
        for lane in ("short", "analysis", "governed")
    }
    mode_deadline = (
        max(lane_budgets.values()) + DEADLINE_MARGIN_SECONDS
        if args.routing
        else DEFAULT_WORKER_SECONDS + DEADLINE_MARGIN_SECONDS
    )
    protocol = evaluation_protocol(
        workers=args.workers,
        max_output_tokens=args.max_output_tokens,
        max_tokens=args.max_tokens,
        hard_case_deadline=mode_deadline,
        steps=max(lane_steps.values()) if args.routing else DEFAULT_WORKER_STEPS,
        seconds=max(lane_budgets.values()) if args.routing else DEFAULT_WORKER_SECONDS,
    )
    protocol["version"] = args.evaluation_protocol
    protocol["request_interval_seconds"] = args.request_interval_seconds
    protocol["hard_case_deadline_mode"] = "per_lane" if args.routing else "fixed"
    protocol["max_repairs_per_case"] = (
        max(lane_repairs.values()) if args.routing else DEFAULT_WORKER_REPAIRS
    )
    if args.routing:
        # Recorded so a reader can recompute each case's limits from its lane.
        protocol["lane_time_budgets"] = lane_budgets
        protocol["lane_step_budgets"] = lane_steps
        protocol["lane_repair_budgets"] = lane_repairs
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
    # Experience reuse changes the information the model receives, so it is part
    # of the protocol. Two runs that differ here are not a paired comparison.
    protocol["experience_reuse"] = args.experience_database is not None
    experience_database = None
    if args.experience_database is not None:
        experience_database = args.output / "experience-snapshot.sqlite"
        source_uri = args.experience_database.resolve().as_uri() + "?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True, timeout=5)) as source,
            closing(sqlite3.connect(experience_database)) as snapshot,
        ):
            source.backup(snapshot)
        # The source can change after the early audit. Audit the frozen copy
        # that workers actually read, and report evidence from that copy.
        try:
            experience_audit = ExperienceStore(
                experience_database, clock=lambda: None, read_only=True
            ).audit()
        except (sqlite3.OperationalError, ExperienceError, FileNotFoundError) as exc:
            parser.error(f"experience snapshot is not a usable experience store: {exc}")
        if not experience_audit["consistent"]:
            parser.error(
                "experience snapshot failed its consistency audit: "
                f"{len(experience_audit['findings'])} finding(s)"
            )
        protocol["experience_store_hash"] = digest(experience_database)
        protocol["experience_scope_hash"] = experience_scope_hash
    if args.preflight_report:
        preflight = json.loads(args.preflight_report.read_text())
        readiness = service_readiness(preflight)
        expected_protocol = hashlib.sha256(
            json.dumps(protocol, sort_keys=True).encode()
        ).hexdigest()
        for key, expected in {
            "benchmark": selected[0].task.benchmark,
            "protocol_hash": expected_protocol,
            "model_route": route,
            "source_manifest_hash": digest(root / "sources.json"),
        }.items():
            if preflight.get(key) != expected:
                readiness["blocked_by"].append("incomparable_preflight_" + key)
        readiness["ready"] = not readiness["blocked_by"]
        readiness.update(
            preflight_sha256=digest(args.preflight_report),
            planned_n=args.limit,
            new_model_calls=0,
        )
        (args.output / "service-preflight.json").write_text(json.dumps(readiness, indent=2) + "\n")
        if not readiness["ready"]:
            print(json.dumps(readiness, indent=2), flush=True)
            raise SystemExit(2)
    from generaldataagent.evaluation.stage_eight import stage8_fingerprint
    from generaldataagent.evaluation.stage_nine import stage9_fingerprint
    from generaldataagent.evaluation.stage_seven import stage7_fingerprint

    case_routes = (
        {c.task.case_id: route_task(c.task.question, c.task.instructions) for c in selected}
        if args.routing
        else None
    )
    routing_manifest = (
        {
            "mode": "public-text-marker",
            "source_hash": digest(ROOT / "src/generaldataagent/evaluation/routing.py"),
            "lanes": {
                lane: route_task("x", override=lane).as_dict()
                for lane in ("short", "analysis", "governed")
            },
            "per_case": {case_id: plan.as_dict() for case_id, plan in case_routes.items()},
        }
        if args.routing
        else None
    )
    active_profiles = (
        {plan["tool_profile"] for plan in routing_manifest["per_case"].values()}
        if routing_manifest is not None
        else {args.tool_profile}
    )
    system_fingerprint = {
        "stage9_sources": stage9_fingerprint(ROOT) if "sql-rules" in active_profiles else None,
        "stage8_sources": stage8_fingerprint(ROOT) if "sql-science" in active_profiles else None,
        "prompt_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "agent_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six.py"),
        "analysis_tools_hash": digest(
            ROOT / "src/generaldataagent/evaluation/stage_six_analysis.py"
        ),
        "pearson_test_hash": digest(ROOT / "src/generaldataagent/tools/pearson_test.py"),
        "independent_test_hash": digest(ROOT / "src/generaldataagent/tools/independent_test.py"),
        "independent_test_contract_hash": digest(
            ROOT / "src/generaldataagent/contracts/independent_test.py"
        ),
        "analysis_contract_hash": digest(ROOT / "src/generaldataagent/contracts/analysis.py"),
        "statistical_public_fields_hash": digest(
            ROOT / "src/generaldataagent/catalog/field_evidence.py"
        ),
        "statistical_rules_hash": digest(
            ROOT / "src/generaldataagent/contracts/statistical_rules.py"
        ),
        "precision_tools_hash": digest(
            ROOT / "src/generaldataagent/evaluation/stage_six_sql_precision.py"
        ),
        "service_control_hash": digest(
            ROOT / "src/generaldataagent/evaluation/stage_six_control.py"
        ),
        "repair_tools_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six_repair.py"),
        "repair_budget_hash": digest(ROOT / "src/generaldataagent/evaluation/repair_budget.py"),
        "retry_policy_hash": digest(ROOT / "src/generaldataagent/evaluation/routing.py"),
        "experience_extraction_hash": digest(
            ROOT / "src/generaldataagent/evaluation/experience_extraction.py"
        )
        if "sql-rules" in active_profiles
        else None,
        "model_adapter_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six_models.py"),
        "worker_hash": digest(ROOT / "src/generaldataagent/evaluation/stage_six_worker.py"),
        "dependency_lock_hash": digest(ROOT / "uv.lock"),
        "tool_profile": "routed" if args.routing else args.tool_profile,
        "routing": routing_manifest,
        "experience": {
            "enabled": args.experience_database is not None,
            "scope_hash": experience_scope_hash,
            "source_hash": digest(ROOT / "src/generaldataagent/business/experience.py")
            if args.experience_database is not None
            else None,
        },
        "stage7_sources": stage7_fingerprint(ROOT)
        if active_profiles
        & {"sql-semantic", "sql-semantic-plan", "sql-semantic-json", "sql-science", "sql-rules"}
        else None,
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
        "tool_profile": "routed" if args.routing else args.tool_profile,
        "provider_id": provider.provider_id,
        "results": [],
        "scope": "bounded SQL evaluation baseline; not production Supervisor or Python DS agent",
        "contamination": "public development data; no training-contamination guarantee",
        "production_accepted": False,
    }
    if experience_audit is not None:
        # Recorded with the run so a later reader can see the store passed only
        # an internal consistency check, not source qualification.
        report["experience_preflight"] = {
            "records": experience_audit["records"],
            "consistent": experience_audit["consistent"],
            "findings": len(experience_audit["findings"]),
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
            route_plan = case_routes[case.task.case_id] if case_routes is not None else None
            result = run_isolated(
                case.task,
                database,
                app_type=args.app_type,
                provider_id=provider.provider_id,
                documents=docs,
                model_override=args.model,
                transport=args.transport,
                route_hash=route["route_hash"],
                tool_profile=route_plan.tool_profile if route_plan else args.tool_profile,
                thinking_mode=args.thinking,
                json_mode=args.json_mode,
                max_output_tokens=args.max_output_tokens,
                max_tokens=args.max_tokens,
                request_interval_seconds=args.request_interval_seconds,
                pacing_database=ROOT / ".state/stage6/request-pacing.sqlite",
                max_steps=route_plan.max_steps if route_plan else DEFAULT_WORKER_STEPS,
                max_seconds=route_plan.max_seconds if route_plan else DEFAULT_WORKER_SECONDS,
                max_repairs=route_plan.max_repairs if route_plan else DEFAULT_WORKER_REPAIRS,
                experience_database=experience_database,
                experience_actor=experience_actor,
                experience_store_hash=protocol.get("experience_store_hash"),
            )
            if route_plan is not None:
                result["route"] = route_plan.as_dict()
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
