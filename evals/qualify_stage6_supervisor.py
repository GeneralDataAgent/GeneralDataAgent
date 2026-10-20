"""Exercise the actual governed Supervisor on TLC; separate from public benchmark scores."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from langchain_core.rate_limiters import InMemoryRateLimiter

from generaldataagent.agents import MetricSupervisorRuntime
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactRef, MetricQuery
from generaldataagent.datasets import build_tlc_metric_catalog
from generaldataagent.evaluation.stage_six import digest, equivalent_rows
from generaldataagent.evaluation.stage_six_models import (
    build_evaluation_model,
    resolve_evaluation_model,
)
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


class RecordedMetricRuntime(MetricAnalysisRuntime):
    def run(self, query, actor):
        result = super().run(query, actor)
        self.recorded.append((query, actor, result))
        return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=3, choices=range(1, 4))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    database = root / "data/tlc/tlc-2025-01.sqlite"
    before = digest(database)
    if args.output.exists():
        raise FileExistsError("evaluation output already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    provider, route = resolve_evaluation_model(
        app_type="claude",
        provider_id=args.provider_id,
        model_override=args.model,
        transport="openai-chat",
        thinking_mode="off",
        json_mode=False,
    )
    model = build_evaluation_model(provider, route)
    model.rate_limiter = InMemoryRateLimiter(requests_per_second=0.625, max_bucket_size=1)
    store = LocalArtifactStore(args.output.parent / "supervisor-artifacts")
    catalog = build_tlc_metric_catalog()
    runtime = RecordedMetricRuntime(
        catalog=catalog,
        executor=SQLiteReadOnlyExecutor(database, max_rows=100),
        artifacts=store,
    )
    runtime.recorded = []
    oracle = MetricAnalysisRuntime(
        catalog=catalog,
        executor=SQLiteReadOnlyExecutor(database, max_rows=100),
        artifacts=LocalArtifactStore(args.output.parent / "oracle-artifacts"),
    )
    supervisor = MetricSupervisorRuntime(model=model, catalog=catalog, metric_runtime=runtime)
    cases = [
        json.loads(line) for line in (root / "evals/datasets/stage1.jsonl").read_text().splitlines()
    ][: args.limit]
    report = {
        "runner_hash": digest(Path(__file__)),
        "supervisor_hash": digest(root / "src/generaldataagent/agents/metric_supervisor.py"),
        "cost_usd": None,
        "evaluation": "actual-governed-supervisor-tlc",
        "model_route": route,
        "database_hash": before,
        "source_cases_hash": digest(root / "evals/datasets/stage1.jsonl"),
        "public_benchmark_score": False,
        "production_accepted": False,
        "results": [],
    }
    for case in cases:
        query = MetricQuery.model_validate(case["query"])
        actor = ActorContext(
            user_id="stage6-evaluation",
            tenant_id="evaluation",
            project_id="tlc",
            data_domains=tuple(case["actor_domains"]),
        )
        expected = oracle.run(query, actor)["delivery_package"]
        runtime.recorded.clear()
        started = time.monotonic()
        result = await supervisor.arun(
            query, actor, thread_id="stage6-" + str(uuid4()), max_runtime_seconds=60
        )
        exact_request = bool(runtime.recorded) and all(
            q == query and a == actor for q, a, _ in runtime.recorded
        )
        matching = False
        evidence_verified = False
        if len(runtime.recorded) == 1:
            actual = runtime.recorded[0][2]["delivery_package"]
            matching = actual["columns"] == expected["columns"] and equivalent_rows(
                actual["rows"], expected["rows"]
            )
            references = [ArtifactRef.model_validate(ref) for ref in actual["evidence"]]
            for ref in references:
                store.read_bytes(ref)
            evidence_verified = bool(references) and set(result.artifact_uris) == {
                ref.uri for ref in references
            }
        roles_complete = result.delegated_roles == (
            "discovery",
            "data-analyst",
            "verifier",
            "reporter",
        )
        row = {
            "case_id": case["case_id"],
            "query": query.model_dump(mode="json"),
            "supervisor": result.model_dump(mode="json"),
            "elapsed_seconds": time.monotonic() - started,
            "executor_calls": len(runtime.recorded),
            "exact_request": exact_request,
            "matches_deterministic_oracle": matching,
            "evidence_hashes_verified": evidence_verified,
            "roles_complete": roles_complete,
            "passed": result.status == "completed"
            and exact_request
            and matching
            and evidence_verified
            and roles_complete,
        }
        report["results"].append(row)
        report["database_unchanged"] = digest(database) == before
        report["summary"] = {
            "n": len(report["results"]),
            "passed": sum(r["passed"] for r in report["results"]),
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in (
                        "case_id",
                        "passed",
                        "executor_calls",
                        "roles_complete",
                        "elapsed_seconds",
                    )
                }
            ),
            flush=True,
        )
    raise SystemExit(
        0 if report["summary"]["passed"] == len(cases) and report["database_unchanged"] else 1
    )


if __name__ == "__main__":
    asyncio.run(main())
