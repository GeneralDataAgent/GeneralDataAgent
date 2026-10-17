import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.data_science import DataScienceWorkbench
from generaldataagent.observability.verified_audit import (
    AuditReceipt,
    VerifiedAuditSink,
    receipt_statement,
)
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.security.source import NativeScopeProof, ScopedQueryExecutor, SourceLease
from generaldataagent.tools.sqlite import QueryResult


@pytest.fixture
def protected_saver(tmp_path):
    with open_protected_checkpointer(tmp_path / "checkpoints", encryption_key=b"c" * 32) as saver:
        yield saver


def test_secured_analysis_propagates_actor_source_lease_native_controls_and_artifact_scope(
    tmp_path,
    protected_saver,
    monkeypatch,
):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("commerce",))
    broker = SQLiteSessionBroker(tmp_path / "sessions")
    private = Ed25519PrivateKey.generate()
    events = []

    def transport(content):
        value = json.loads(content)
        events.append(value)
        receipt = AuditReceipt(
            event_id=value["event_id"],
            event_sha256=hashlib.sha256(content).hexdigest(),
            sequence=len(events),
            recorded_at=datetime.now(UTC),
            signer_id="audit",
            signature="",
        )
        return receipt.model_copy(
            update={
                "signature": base64.b64encode(private.sign(receipt_statement(receipt))).decode()
            }
        )

    audit = VerifiedAuditSink(
        transport=transport, trusted_keys={"audit": private.public_key()}, pseudonym_key=b"x" * 32
    )
    seen = []

    def lease_provider(value):
        seen.append(value)
        now = datetime.now(UTC)
        return SourceLease(
            actor=value,
            source_id="warehouse",
            role="scoped-reader",
            credential="ephemeral-token",
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
            allowed_relations=frozenset({"orders"}),
        )

    class Session:
        def inspect_native_scope(self):
            return NativeScopeProof(
                actor=actor,
                source_id="warehouse",
                role="scoped-reader",
                row_security_enforced=True,
                column_security_enforced=True,
                read_only=True,
                superuser=False,
                bypass_rls=False,
            )

        def execute(self, sql, parameters):
            from langsmith.run_helpers import get_tracing_context

            assert get_tracing_context()["enabled"] is False
            assert "orders" in sql
            return QueryResult(("metric_value",), ((3,),), 1.0, ())

    @contextmanager
    def opened(lease):
        assert lease.actor == actor
        yield Session()

    executor = ScopedQueryExecutor(lease_provider=lease_provider, open_session=opened)
    artifacts = GovernedArtifactStore(
        tmp_path / "content", index_path=tmp_path / "index", actor=actor
    )
    catalog = InMemoryMetricCatalog(
        [
            MetricDefinition(
                name="order_count",
                version="v1",
                description="orders",
                data_domain="commerce",
                relation="orders",
                expression="COUNT(*)",
                time_column="created_at",
                owner="data",
            )
        ]
    )
    runtime = MetricAnalysisRuntime(
        catalog=catalog,
        executor=executor,
        artifacts=artifacts,
        audit=audit,
        production_mode=True,
        budgets=TaskBudgetLedger(tmp_path / "budgets"),
        checkpointer=protected_saver,
    )
    result = runtime.run(MetricQuery(metric_name="order_count", metric_version="v1"), actor)
    assert result["status"] == "completed"
    assert seen == [actor]
    assert runtime.resume(result["task_id"], actor)["task_id"] == result["task_id"]
    assert seen == [actor]
    with pytest.raises(PermissionError):
        runtime.resume(result["task_id"], actor.model_copy(update={"user_id": "other"}))
    assert any("request_fingerprint" in event["payload"] for event in events)
    with pytest.raises(PermissionError, match="artifact_store_actor"):
        runtime.run(
            MetricQuery(metric_name="order_count", metric_version="v1"),
            actor.model_copy(update={"tenant_id": "foreign"}),
        )
    api = TestClient(
        create_app(metric_runtime=runtime, session_broker=broker, production_mode=True)
    )
    token = broker.issue(
        user_id="u",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"commerce"}),
        permissions=frozenset({"analysis:run"}),
    )
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "u",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "commerce",
    }
    response = api.post(
        "/v1/analysis/metrics",
        headers=headers,
        json={"metric_name": "order_count", "metric_version": "v1"},
    )
    assert response.status_code == 200
    resumed = api.post(f"/v1/analysis/tasks/{response.json()['task_id']}/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["task_id"] == response.json()["task_id"]
    assert len(seen) == 2
    with pytest.raises(ValueError):
        MetricAnalysisRuntime(
            catalog=catalog,
            executor=executor,
            artifacts=LocalArtifactStore(tmp_path / "unsafe"),
            production_mode=True,
        )
    with pytest.raises(ValueError):
        create_app(
            science_workbench=DataScienceWorkbench(artifact_store=artifacts),
            session_broker=broker,
            production_mode=True,
        )
    with pytest.raises(ValueError):
        DataScienceWorkbench(artifact_store=artifacts, production_mode=True)
