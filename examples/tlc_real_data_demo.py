"""Run certified metric analysis against a real NYC TLC monthly snapshot."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.datasets import (
    build_tlc_metric_catalog,
    download_tlc_sources,
    prepare_tlc_database,
)
from generaldataagent.llm import CcSwitchProvider, LangChainNarrator, OpenAiCcSwitchProvider
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def build_runtime(
    root: Path,
    *,
    provider_id: str | None = None,
    app_type: str = "claude",
    timeout_seconds: float = 30,
) -> MetricAnalysisRuntime:
    raw_dir = root / "data" / "tlc" / "raw"
    database = root / "data" / "tlc" / "tlc-2025-01.sqlite"
    trips, zones = download_tlc_sources(raw_dir)
    manifest = prepare_tlc_database(trips, zones, database)
    print(f"Prepared {manifest.row_count:,} real trip records ({manifest.period}).")

    metrics = build_tlc_metric_catalog()
    provider = (
        CcSwitchProvider.load(provider_id=provider_id)
        if app_type == "claude"
        else OpenAiCcSwitchProvider.load(provider_id=provider_id)
    )
    return MetricAnalysisRuntime(
        catalog=metrics,
        executor=SQLiteReadOnlyExecutor(database, max_rows=100),
        artifacts=LocalArtifactStore(root / ".artifacts" / "tlc-demo"),
        audit=JsonlAuditSink(root / ".state" / "tlc-demo" / "audit.jsonl"),
        narrator=LangChainNarrator(
            provider.build_chat_model(timeout_seconds=timeout_seconds, max_retries=0),
            model_name=provider.model,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider-id",
        help="Stable CC Switch provider id; defaults to its current Claude provider.",
    )
    parser.add_argument("--app-type", choices=("claude", "codex"), default="claude")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = build_runtime(
        root,
        provider_id=arguments.provider_id,
        app_type=arguments.app_type,
        timeout_seconds=arguments.timeout_seconds,
    ).run(
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
            project_id="tlc-validation",
            data_domains=("mobility.tlc",),
        ),
        thread_id="tlc-real-data-demo",
    )
    delivery = result["delivery_package"]
    print(f"Model: {delivery['narrative_model']}")
    print(delivery["summary"])
    for row in delivery["rows"]:
        print(row)


if __name__ == "__main__":
    main()
