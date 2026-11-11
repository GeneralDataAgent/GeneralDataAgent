import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime, query

from generaldataagent.api.app import create_app
from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.orchestration.task_events import TaskEventJournal
from generaldataagent.orchestration.task_queue import (
    MetricTaskQueue,
    TaskLeaseLost,
    TaskQueueConflict,
)
from generaldataagent.orchestration.task_service import MetricTaskService
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.identity import SQLiteSessionBroker


def credentials(broker, owner=None, permissions=frozenset({"analysis:run"})):
    owner = owner or actor()
    token = broker.issue(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        data_domains=frozenset(owner.data_domains),
        permissions=permissions,
        ttl_seconds=900,
    )
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": owner.user_id,
        "x-tenant-id": owner.tenant_id,
        "x-project-id": owner.project_id,
        "x-data-domain": owner.data_domains[0],
        "idempotency-key": "submission-1",
    }
    return token, headers


@contextmanager
def assembly(
    tmp_path,
    original,
    *,
    clock=None,
    runtime_type=MetricAnalysisRuntime,
    max_attempts=3,
    max_pending=100,
    lease_seconds=60,
    feedback=None,
    question_catalog=None,
):
    events = TaskEventJournal(tmp_path / "events.db", clock=clock)
    budgets = TaskBudgetLedger(tmp_path / "budgets.db", clock=clock)
    broker = SQLiteSessionBroker(tmp_path / "sessions.db", clock=clock)
    queue = MetricTaskQueue(
        tmp_path / "jobs.db",
        encryption_key=b"q" * 32,
        clock=clock,
        max_attempts=max_attempts,
        max_pending_per_actor=max_pending,
        lease_seconds=lease_seconds,
    )
    with open_protected_checkpointer(
        tmp_path / "checkpoint.db", encryption_key=b"c" * 32, clock=clock
    ) as saver:

        def factory(execution):
            claim = execution.claim
            return runtime_type(
                catalog=original.catalog,
                executor=original.executor,
                artifacts=original.artifacts,
                checkpointer=saver,
                task_events=events,
                execution_guard=execution.guard,
                composition=ExecutionComposition(
                    "research",
                    claim.actor,
                    original.artifacts,
                    original.audit,
                    budgets,
                    claim.record.task_id,
                    str(claim.record.task_id),
                ),
            )

        service = MetricTaskService(
            queue=queue,
            events=events,
            broker=broker,
            budgets=budgets,
            runtime_factory=factory,
            feedback=feedback,
            question_catalog=question_catalog,
        )
        yield service, saver


def test_api_returns_durable_task_before_sql_and_worker_completes_same_task(tmp_path):
    original = build_runtime(tmp_path)
    calls = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        calls.append("sql")
        return execute(*args, **kwargs)

    original.executor.execute = counted
    with assembly(tmp_path, original) as (service, saver):
        token, headers = credentials(service.broker)
        client = TestClient(
            create_app(
                session_broker=service.broker, task_events=service.events, metric_tasks=service
            )
        )
        submitted = client.post(
            "/v1/analysis/tasks", headers=headers, json=query().model_dump(mode="json")
        )
        assert submitted.status_code == 202
        record = submitted.json()
        assert record["status"] == "queued" and record["attempts"] == 0
        assert record["task_id"] != record["thread_id"]
        assert calls == []
        url = f"/v1/analysis/tasks/{record['task_id']}"
        assert client.get(url, headers=headers).json()["status"] == "queued"
        assert len(client.get(url + "/events", headers=headers).json()["events"]) == 1
        repeated = client.post(
            "/v1/analysis/tasks", headers=headers, json=query().model_dump(mode="json")
        )
        assert repeated.json() == record
        completed = service.run_once()
        assert completed.task_id == UUID(record["task_id"])
        assert completed.status == "completed" and completed.attempts == 1
        assert calls == ["sql"]
        assert client.get(url, headers=headers).json()["status"] == "completed"
        assert service.run_once() is None
        assert client.post(url + "/retry", headers=headers).status_code == 409
        assert client.get(url + "/events", headers=headers).json()["latest_sequence"] == 7
        state = saver.get_tuple({"configurable": {"thread_id": record["thread_id"]}})
        assert state.checkpoint["channel_values"]["task_id"] == record["task_id"]
        assert token.encode() not in (tmp_path / "jobs.db").read_bytes()
        assert b"order_count" not in (tmp_path / "jobs.db").read_bytes()
        assert b"user-1" not in (tmp_path / "jobs.db").read_bytes()


def test_restart_resumes_only_failed_delivery_and_preserves_budget_usage(tmp_path):
    original = build_runtime(tmp_path)
    calls = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        calls.append("sql")
        return execute(*args, **kwargs)

    class FailingDelivery(MetricAnalysisRuntime):
        def _deliver(self, state):
            raise RuntimeError("private failure body")

    original.executor.execute = counted
    with assembly(tmp_path, original, runtime_type=FailingDelivery) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "restart-1")
        interrupted = service.run_once()
        assert interrupted.status == "interrupted"
        assert interrupted.failure_code == "execution_interrupted"
        with sqlite3.connect(service.budgets.path) as db:
            before = db.execute("SELECT started, calls FROM task_budgets").fetchone()
    with assembly(tmp_path, original) as (reopened, _):
        # The persisted broker still recognizes the unexpired original grant after restart.
        assert reopened.read(record.task_id, actor(), token).status == "interrupted"
        new_token, _ = credentials(reopened.broker)
        assert reopened.retry(record.task_id, actor(), new_token).status == "queued"
        completed = reopened.run_once()
        assert completed.status == "completed" and completed.attempts == 2
        assert calls == ["sql"]
        with sqlite3.connect(reopened.budgets.path) as db:
            assert db.execute("SELECT started, calls FROM task_budgets").fetchone() == before
        events = reopened.events.read(record.task_id, actor()).events
        assert [e.phase for e in events].count("deliver") == 2
        assert "private failure body" not in json.dumps(completed.model_dump(mode="json"))


def test_revocation_and_expiry_stop_queued_work_and_new_session_can_retry(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, clock=lambda: now) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "revoked")
        service.broker.revoke(token)
        assert service.run_once().failure_code == "authorization_unavailable"
        assert not list((tmp_path / "artifacts").rglob("*.json"))
        fresh, _ = credentials(service.broker)
        service.retry(record.task_id, actor(), fresh)
        assert service.run_once().status == "completed"
        record2 = service.submit(query(), actor(), fresh, "expired")
        now += timedelta(seconds=900)
        assert service.run_once().status == "interrupted"
        with pytest.raises(PermissionError):
            service.read(record2.task_id, actor(), fresh)


def test_revocation_during_sql_prevents_checkpointing_or_delivery(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, saver):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "during-sql")
        execute = original.executor.execute

        def revoking(*args, **kwargs):
            result = execute(*args, **kwargs)
            service.broker.revoke(token)
            return result

        original.executor.execute = revoking
        assert service.run_once().failure_code == "authorization_unavailable"
        state = saver.get_tuple({"configurable": {"thread_id": str(record.thread_id)}})
        assert "analysis_result" not in state.checkpoint["channel_values"]
        assert not service.events.read(record.task_id, actor()).terminal


def test_scope_idempotency_and_live_permission_boundaries(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        client = TestClient(
            create_app(
                session_broker=service.broker, task_events=service.events, metric_tasks=service
            )
        )
        payload = query().model_dump(mode="json")
        record = client.post("/v1/analysis/tasks", headers=headers, json=payload).json()
        url = f"/v1/analysis/tasks/{record['task_id']}"
        changed = client.post(
            "/v1/analysis/tasks", headers=headers, json=payload | {"dimensions": []}
        )
        assert changed.status_code == 409
        for update in (
            {"user_id": "other"},
            {"tenant_id": "other"},
            {"project_id": "other"},
            {"data_domains": ("other",)},
        ):
            stranger = ActorContext.model_validate(actor().model_dump() | update)
            _, other_headers = credentials(service.broker, stranger)
            assert client.get(url, headers=other_headers).status_code == 404
            assert client.post(url + "/retry", headers=other_headers).status_code == 404
        _, denied = credentials(service.broker, permissions=frozenset())
        assert client.post("/v1/analysis/tasks", headers=denied, json=payload).status_code == 403
        assert client.get(url, headers={}).status_code == 401
        assert (
            client.post(
                "/v1/analysis/tasks", headers=headers | {"idempotency-key": "bad key"}, json=payload
            ).status_code
            == 422
        )
        reference = service.broker.session_reference(token)
        with pytest.raises(PermissionError):
            service.broker.authenticate(reference)


def test_concurrent_submissions_and_claims_do_not_duplicate_work(tmp_path):
    queue = MetricTaskQueue(tmp_path / "jobs.db", encryption_key=b"q" * 32)
    reference = "a" * 64
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(lambda _: queue.submit(query(), actor(), reference, "same"), range(20))
        )
    assert len({r.task_id for r in records}) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: queue.claim(), range(20)))
    assert len([c for c in claims if c]) == 1


def test_expired_lease_is_fenced_and_attempt_budget_is_bounded(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    queue = MetricTaskQueue(
        tmp_path / "jobs.db",
        encryption_key=b"q" * 32,
        clock=lambda: now,
        lease_seconds=2,
        max_attempts=2,
    )
    record = queue.submit(query(), actor(), "a" * 64, "lease")
    old = queue.claim()
    now += timedelta(seconds=2)
    current = queue.claim()
    assert current.record.task_id == old.record.task_id
    assert current.lease_token != old.lease_token
    for action in (
        lambda: queue.renew(old),
        lambda: queue.settle(old, "completed"),
        lambda: queue.check(old),
    ):
        with pytest.raises(TaskLeaseLost):
            action()
    failed = queue.settle(current, "interrupted", "execution_interrupted")
    assert failed.status == "failed" and failed.failure_code == "attempts_exhausted"
    with pytest.raises(TaskQueueConflict):
        queue.retry(record.task_id, actor(), "b" * 64)
    assert queue.claim() is None


def test_tampered_payload_wrong_key_and_expiry_are_not_treated_as_new_work(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    path = tmp_path / "jobs.db"
    queue = MetricTaskQueue(path, encryption_key=b"q" * 32, clock=lambda: now, retention_seconds=2)
    record = queue.submit(query(), actor(), "a" * 64, "sealed")
    wrong = MetricTaskQueue(path, encryption_key=b"z" * 32, clock=lambda: now)
    with pytest.raises(PermissionError):
        wrong.read(record.task_id, actor())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metric_jobs SET request_hash = 'changed'")
    with pytest.raises(PermissionError):
        queue.claim()
    now += timedelta(seconds=2)
    with pytest.raises(PermissionError):
        queue.read(record.task_id, actor())
    assert queue.purge_expired() == 1
    assert queue.claim() is None


def test_pending_limits_and_terminal_jobs_cannot_be_restarted(tmp_path):
    queue = MetricTaskQueue(tmp_path / "jobs.db", encryption_key=b"q" * 32, max_pending_per_actor=1)
    record = queue.submit(query(), actor(), "a" * 64, "one")
    with pytest.raises(TaskQueueConflict):
        queue.submit(query(), actor(), "a" * 64, "two")
    queue.settle(queue.claim(), "completed")
    assert queue.submit(query(), actor(), "a" * 64, "one").task_id == record.task_id
    with pytest.raises(TaskQueueConflict):
        queue.retry(record.task_id, actor(), "b" * 64)
    assert queue.submit(query(), actor(), "a" * 64, "two").task_id != record.task_id


def test_acceptance_event_gap_and_registered_checkpoint_gap_are_repaired(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, saver):
        token, _ = credentials(service.broker)
        reference = service.broker.session_reference(token)
        # Simulate process death after durable acceptance, before journal start and graph input.
        record = service.queue.submit(query(), actor(), reference, "gap")
        saver.register_thread(str(record.thread_id), actor())
        assert service.run_once().status == "completed"
        assert service.events.read(record.task_id, actor()).terminal


def test_missing_ports_and_default_api_are_fail_closed(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        with pytest.raises(ValueError, match="identity_broker"):
            create_app(metric_tasks=service)
        with pytest.raises(ValueError, match="event_journal"):
            create_app(metric_tasks=service, session_broker=service.broker)
        with pytest.raises(ValueError, match="production"):
            create_app(
                metric_tasks=service,
                session_broker=service.broker,
                task_events=service.events,
                production_mode=True,
            )
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "bad-ports")
        service.runtime_factory = lambda _: original
        assert service.run_once().status == "failed"
        assert service.queue.read(record.task_id, actor()).failure_code == "invalid_task"
    assert (
        TestClient(create_app())
        .post(
            "/v1/analysis/tasks",
            headers={"idempotency-key": "one"},
            json=query().model_dump(mode="json"),
        )
        .status_code
        == 503
    )


@pytest.mark.parametrize(
    "options",
    [
        {"encryption_key": b"short"},
        {"lease_seconds": True},
        {"max_attempts": 0},
        {"retention_seconds": 0},
    ],
)
def test_invalid_queue_configuration(tmp_path, options):
    with pytest.raises(ValueError):
        MetricTaskQueue(tmp_path / "jobs.db", **({"encryption_key": b"q" * 32} | options))


def test_real_worker_heartbeat_keeps_claim_live_while_sql_is_blocked(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, lease_seconds=2) as (service, _):
        token, _ = credentials(service.broker)
        service.submit(query(), actor(), token, "heartbeat")
        renewed = Event()
        renew = service.queue.renew
        execute = original.executor.execute

        def observed_renew(claim):
            renew(claim)
            renewed.set()

        def blocked(*args, **kwargs):
            assert renewed.wait(4)
            return execute(*args, **kwargs)

        service.queue.renew = observed_renew
        original.executor.execute = blocked
        assert service.run_once().status == "completed"
        assert renewed.is_set()


def test_lost_worker_cannot_checkpoint_or_settle_after_sql_returns(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, clock=lambda: now, lease_seconds=2) as (service, saver):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "fenced-worker")
        execute = original.executor.execute
        replacements = []

        def replace(*args, **kwargs):
            nonlocal now
            result = execute(*args, **kwargs)
            now += timedelta(seconds=2)
            replacements.append(service.queue.claim())
            return result

        original.executor.execute = replace
        with pytest.raises(TaskLeaseLost):
            service.run_once()
        assert service.queue.read(record.task_id, actor()).attempts == 2
        service.queue.check(replacements[0])
        state = saver.get_tuple({"configurable": {"thread_id": str(record.thread_id)}})
        assert "analysis_result" not in state.checkpoint["channel_values"]


def test_retry_does_not_reset_exhausted_runtime_budget(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    original = build_runtime(tmp_path)

    class FailDelivery(MetricAnalysisRuntime):
        def _deliver(self, state):
            raise RuntimeError("delivery unavailable")

    with assembly(tmp_path, original, clock=lambda: now, runtime_type=FailDelivery) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "budget")
        assert service.run_once().status == "interrupted"
        now += timedelta(seconds=601)
        service.retry(record.task_id, actor(), token)
        result = service.run_once()
        assert result.status == "failed" and result.failure_code == "budget_exhausted"


def test_switching_to_empty_checkpoint_store_cannot_reexecute_old_sql(tmp_path):
    original = build_runtime(tmp_path)
    calls = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        calls.append("sql")
        return execute(*args, **kwargs)

    class FailDelivery(MetricAnalysisRuntime):
        def _deliver(self, state):
            raise RuntimeError("delivery unavailable")

    original.executor.execute = counted
    with assembly(tmp_path, original, runtime_type=FailDelivery) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "storage-replacement")
        assert service.run_once().status == "interrupted"
        service.retry(record.task_id, actor(), token)
        # A trusted deployment error points to a new empty checkpoint store.
        with open_protected_checkpointer(tmp_path / "empty.db", encryption_key=b"c" * 32) as empty:

            def wrong_factory(execution):
                claim = execution.claim
                return MetricAnalysisRuntime(
                    catalog=original.catalog,
                    executor=original.executor,
                    artifacts=original.artifacts,
                    checkpointer=empty,
                    task_events=service.events,
                    execution_guard=execution.guard,
                    composition=ExecutionComposition(
                        "research",
                        claim.actor,
                        original.artifacts,
                        original.audit,
                        service.budgets,
                        claim.record.task_id,
                        str(claim.record.task_id),
                    ),
                )

            service.runtime_factory = wrong_factory
            assert service.run_once().status == "failed"
            assert calls == ["sql"]


def test_worker_fence_is_rechecked_at_checkpoint_write_after_node_returns(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    original = build_runtime(tmp_path)
    replacements = []

    class LoseLeaseAfterNode(MetricAnalysisRuntime):
        def _observed(self, handler, phase):
            wrapped = super()._observed(handler, phase)

            def after_node(state):
                nonlocal now
                result = wrapped(state)
                if phase == "analyze":
                    # Move the queue fence after the node's own post-call guard succeeded.
                    now += timedelta(seconds=2)
                    replacements.append(service.queue.claim())
                return result

            return after_node

    with assembly(
        tmp_path, original, clock=lambda: now, lease_seconds=2, runtime_type=LoseLeaseAfterNode
    ) as (service, saver):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "checkpoint-fence")
        with pytest.raises(TaskLeaseLost):
            service.run_once()
        service.queue.check(replacements[0])
        snapshot = saver.get_tuple({"configurable": {"thread_id": str(record.thread_id)}})
        assert "analysis_result" not in snapshot.checkpoint["channel_values"]
