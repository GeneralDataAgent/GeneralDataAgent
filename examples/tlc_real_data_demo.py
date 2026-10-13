"""Run certified metric analysis against a real NYC TLC monthly snapshot."""

from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.datasets import (
    build_tlc_metric_catalog,
    download_tlc_sources,
    prepare_tlc_database,
)
from generaldataagent.llm import CcSwitchProvider, LangChainNarrator
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def build_runtime(root: Path) -> MetricAnalysisRuntime:
    raw_dir = root / "data" / "tlc" / "raw"
    database = root / "data" / "tlc" / "tlc-2025-01.sqlite"
    trips, zones = download_tlc_sources(raw_dir)
    manifest = prepare_tlc_database(trips, zones, database)
    print(f"Prepared {manifest.row_count:,} real trip records ({manifest.period}).")

    metrics = build_tlc_metric_catalog()
    provider = CcSwitchProvider.load()
    return MetricAnalysisRuntime(
        catalog=metrics,
        executor=SQLiteReadOnlyExecutor(database, max_rows=100),
        artifacts=LocalArtifactStore(root / ".artifacts" / "tlc-demo"),
        audit=JsonlAuditSink(root / ".state" / "tlc-demo" / "audit.jsonl"),
        narrator=LangChainNarrator(provider.build_chat_model(), model_name=provider.model),
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    result = build_runtime(root).run(
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
