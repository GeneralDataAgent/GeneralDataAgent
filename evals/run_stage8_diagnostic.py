"""Answer-blind runtime diagnostics on a declared development subset, scored offline."""

import argparse
import json
import subprocess
from pathlib import Path

from generaldataagent.evaluation.stage_eight import stage8_fingerprint
from generaldataagent.evaluation.stage_six import (
    digest,
    load_infi,
    run_isolated,
    score_answers,
    verify_sources,
)
from generaldataagent.evaluation.stage_six_import import build_identifier_preserving_database
from generaldataagent.evaluation.stage_six_models import resolve_evaluation_model

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--case-ids", nargs="+", default=["5", "30", "69", "70", "255", "326", "644", "659"]
    )
    parser.add_argument("--pacing-database", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    if len(set(args.case_ids)) != len(args.case_ids):
        parser.error("diagnostic case IDs must be unique")
    source = ROOT / "data/stage6"
    verify_sources(source)
    cases = {c.task.case_id: c for c in load_infi(source / "infi")}
    if not set(args.case_ids) <= cases.keys():
        parser.error("unknown development cases")
    provider, route = resolve_evaluation_model(
        app_type="magpie",
        provider_id=None,
        model_override="workbuddy/deepseek-v4.1-flash",
        transport="openai-chat",
    )
    report = {
        "scope": "selected public development diagnostics; do not pool into full-cohort scores",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_fingerprints": stage8_fingerprint(ROOT),
        "dependency_lock_hash": digest(ROOT / "uv.lock"),
        "model_route": route,
        "token_caps": None,
        "case_ids": args.case_ids,
        "results": [],
        "production_accepted": False,
        "official_leaderboard_comparable": False,
        "runtime_context": "public task only; reference values used exclusively by offline scorer",
    }
    target = args.output / "report.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    for key in args.case_ids:
        case = cases[key]
        database = args.output / (key + ".sqlite")
        build_identifier_preserving_database(
            {"data": source / "infi/da-dev-tables" / case.task.asset}, database
        )
        original_hash = digest(database)
        result = run_isolated(
            case.task,
            database,
            app_type="magpie",
            provider_id=provider.provider_id,
            model_override=provider.model,
            transport="openai-chat",
            route_hash=route["route_hash"],
            tool_profile="sql-science",
            request_interval_seconds=3.5,
            pacing_database=args.pacing_database or ROOT / ".state/stage6/request-pacing.sqlite",
        )
        passed, hits, total = score_answers(case, result["answers"])
        result.update(
            case_id=key,
            database_hash=original_hash,
            source_unchanged=original_hash == digest(database),
            passed=passed and not result["failure"] and original_hash == digest(database),
            sub_hits=hits,
            sub_total=total,
        )
        report["results"].append(result)
        target.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps({"case_id": key, "passed": result["passed"], "failure": result["failure"]}),
            flush=True,
        )
    report["status"] = "completed"
    report["passed"] = sum(r["passed"] for r in report["results"])
    target.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
