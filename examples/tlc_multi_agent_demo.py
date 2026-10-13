"""Run the real Deep Agents Supervisor over the governed TLC metric tools."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.agents import MetricSupervisorRuntime
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.datasets import (
    build_tlc_metric_catalog,
    download_tlc_sources,
    prepare_tlc_database,
)
from generaldataagent.llm import CcSwitchProvider, OpenAiCcSwitchProvider
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider-id",
        help="Stable CC Switch provider id; defaults to its current Claude provider.",
    )
    parser.add_argument("--app-type", choices=("claude", "codex"), default="claude")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--max-runtime-seconds", type=float, default=60)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    trips, zones = download_tlc_sources(root / "data" / "tlc" / "raw")
    database = root / "data" / "tlc" / "tlc-2025-01.sqlite"
    prepare_tlc_database(trips, zones, database)

    catalog = build_tlc_metric_catalog()
    metric_runtime = MetricAnalysisRuntime(
        catalog=catalog,
        executor=SQLiteReadOnlyExecutor(database, max_rows=100),
        artifacts=LocalArtifactStore(root / ".artifacts" / "tlc-multi-agent"),
        audit=JsonlAuditSink(root / ".state" / "tlc-multi-agent" / "audit.jsonl"),
    )
    provider = (
        CcSwitchProvider.load(provider_id=arguments.provider_id)
        if arguments.app_type == "claude"
        else OpenAiCcSwitchProvider.load(provider_id=arguments.provider_id)
    )
    supervisor = MetricSupervisorRuntime(
        model=provider.build_chat_model(
            timeout_seconds=arguments.timeout_seconds,
            max_retries=0,
        ),
        catalog=catalog,
        metric_runtime=metric_runtime,
    )
    result = supervisor.run(
        MetricQuery(
            metric_name="trip_count",
            metric_version="v1",
            dimensions=("pickup_borough",),
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            end_time=datetime(2025, 2, 1, tzinfo=UTC),
        ),
        ActorContext(
            user_id="local-user",
            tenant_id="local",
            project_id="tlc-multi-agent-validation",
            data_domains=("mobility.tlc",),
        ),
        thread_id="tlc-real-multi-agent-demo",
        max_runtime_seconds=arguments.max_runtime_seconds,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
