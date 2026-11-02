import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime, query
from test_stage11_task_streams import connected, parse_frames
from test_stage11_task_submission import assembly, credentials

from generaldataagent.api.app import create_app
from generaldataagent.orchestration.task_queue import (
    MetricTaskQueue,
    MetricTaskRecord,
    TaskCancellationRequested,
    TaskLeaseLost,
    TaskQueueConflict,
)
from generaldataagent.orchestration.task_streams import MetricTaskStreams
from generaldataagent.tools.sqlite import QueryExecutionError
from generaldataagent.tools.sqlite_guarded import GuardedSQLiteReadOnlyExecutor


def local_queue(tmp_path, **kwargs):
    return MetricTaskQueue(tmp_path / "jobs.db", encryption_key=b"q" * 32, **kwargs)


def accepted(queue):
    return queue.submit(query(), actor(), "a" * 64, "cancel")


@pytest.mark.parametrize("initial", ["queued", "interrupted"])
def test_inactive_cancel_is_durable_idempotent_and_never_claimed(tmp_path, initial):
    queue = local_queue(tmp_path)
    record = accepted(queue)
    if initial == "interrupted":
        claim = queue.claim()
        queue.settle(claim, "interrupted", "execution_interrupted")
    result = queue.cancel(record.task_id, actor())
    assert result.schema_version == "2.0"
    assert result.status == "cancelled"
    assert result.cancel_requested_at == result.cancelled_at
    assert result.failure_code is None
    reopened = local_queue(tmp_path)
    assert reopened.cancel(record.task_id, actor()) == result
    assert reopened.submit(query(), actor(), "a" * 64, "cancel") == result
    assert reopened.claim() is None
    assert reopened.list_tasks(actor()).tasks == (result,)
    with pytest.raises(TaskQueueConflict, match="not_retryable"):
        reopened.retry(record.task_id, actor(), "b" * 64)
    with pytest.raises(TaskQueueConflict, match="not_ready"):
        reopened.result_context(record.task_id, actor())


@pytest.mark.parametrize("outcome", ["completed", "interrupted", "failed", "needs_human"])
def test_running_cancel_only_acknowledged_at_owned_settlement(tmp_path, outcome):
    queue = local_queue(tmp_path)
    record = accepted(queue)
    claim = queue.claim()
    requested = queue.cancel(record.task_id, actor())
    assert requested.status == "cancel_requested" and requested.cancelled_at is None
    assert queue.cancel(record.task_id, actor()) == requested
    assert queue.claim() is None
    queue.renew(claim)  # The owner retains its lease while unwinding.
    with pytest.raises(TaskCancellationRequested):
        queue.check(claim)
    stopped = queue.settle(claim, outcome)
    assert stopped.status == "cancelled" and stopped.attempts == 1
    assert stopped.cancelled_at >= stopped.cancel_requested_at
    assert stopped.failure_code is None
    with pytest.raises(TaskLeaseLost):
        queue.settle(claim, "completed")


@pytest.mark.parametrize("outcome", ["completed", "failed", "needs_human"])
def test_completion_winning_transaction_is_not_retroactively_cancelled(tmp_path, outcome):
    queue = local_queue(tmp_path)
    record = accepted(queue)
    result = queue.settle(queue.claim(), outcome)
    with pytest.raises(TaskQueueConflict, match="not_cancellable"):
        queue.cancel(record.task_id, actor())
    assert queue.read(record.task_id, actor()) == result


def test_expired_cancel_lease_is_unconfirmed_after_restart_and_counts_against_capacity(tmp_path):
    now = [datetime.now(UTC)]
    queue = local_queue(tmp_path, clock=lambda: now[0], max_pending_per_actor=1)
    record = accepted(queue)
    claim = queue.claim()
    queue.cancel(record.task_id, actor())
    now[0] += timedelta(seconds=61)
    reopened = local_queue(tmp_path, clock=lambda: now[0], max_pending_per_actor=1)
    assert reopened.claim() is None
    assert reopened.read(record.task_id, actor()).status == "cancel_requested"
    with pytest.raises(TaskLeaseLost):
        queue.settle(claim, "completed")
    with pytest.raises(TaskQueueConflict, match="pending_budget"):
        reopened.submit(query(), actor(), "a" * 64, "other")


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "other"),
        ("tenant_id", "other"),
        ("project_id", "other"),
        ("data_domains", ("finance",)),
    ],
)
def test_other_owner_or_domain_cannot_cancel(tmp_path, field, value):
    queue = local_queue(tmp_path)
    record = accepted(queue)
    with pytest.raises(PermissionError):
        queue.cancel(record.task_id, actor().model_copy(update={field: value}))
    assert queue.read(record.task_id, actor()).status == "queued"


def test_api_cancel_requires_live_grant_and_sse_ends_on_cancelled(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "api-cancel")
        client = TestClient(
            create_app(
                session_broker=service.broker, task_events=service.events, metric_tasks=service
            )
        )
        url = f"/v1/analysis/tasks/{record.task_id}"
        assert client.post(url + "/cancel").status_code == 401
        other = actor().model_copy(update={"user_id": "other"})
        _, wrong = credentials(service.broker, other)
        assert client.post(url + "/cancel", headers=wrong).status_code == 404
        assert (
            client.post(f"/v1/analysis/tasks/{uuid4()}/cancel", headers=headers).status_code == 404
        )
        assert client.post(url + "/cancel", headers=headers).json()["status"] == "cancelled"
        response = client.get(url + "/stream", headers=headers)
        frames = parse_frames(response.text)
        assert frames[0]["data"]["schema_version"] == "2.0"
        assert frames[0]["data"]["status"] == "cancelled"
        assert frames[-1]["data"]["reason"] == "terminal"
        assert client.post(url + "/retry", headers=headers).status_code == 409
        assert client.get(url + "/result", headers=headers).status_code == 409
        assert service.run_once() is None  # Zero execution, checkpoint, or budget registration.
        service.broker.revoke(token)
        assert client.post(url + "/cancel", headers=headers).status_code == 401


def test_cancel_endpoint_default_off_and_existing_service_gate(tmp_path):
    assert TestClient(create_app()).post(f"/v1/analysis/tasks/{uuid4()}/cancel").status_code == 503
    with assembly(tmp_path, build_runtime(tmp_path)) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "gate")
        client = TestClient(
            create_app(
                session_broker=service.broker,
                task_events=service.events,
                metric_tasks=service,
                service_enabled=lambda: False,
            )
        )
        assert (
            client.post(f"/v1/analysis/tasks/{record.task_id}/cancel", headers=headers).status_code
            == 503
        )
        assert service.read(record.task_id, actor(), token).status == "queued"


def test_worker_does_not_report_stopped_until_blocked_tool_returns(tmp_path):
    original = build_runtime(tmp_path)
    entered, release = Event(), Event()
    execute = original.executor.execute

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return execute(*args, **kwargs)

    original.executor.execute = blocked
    with assembly(tmp_path, original) as (service, _), ThreadPoolExecutor(1) as pool:
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "blocked")
        worker = pool.submit(service.run_once)
        try:
            assert entered.wait(5)
            result = service.cancel(record.task_id, actor(), token)
            assert result.status == "cancel_requested" and result.cancelled_at is None
            assert not worker.done()
            assert service.run_once() is None
        finally:
            release.set()
        stopped = worker.result(timeout=5)
        assert stopped.status == "cancelled"
        assert stopped.cancelled_at is not None
        assert service.run_once() is None


LONG_SQL = """WITH RECURSIVE n(x) AS (
    VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x < 1000000000
) SELECT sum(x) FROM n"""


def test_cancellation_interrupts_actual_sql_and_worker_acknowledges_after_close(
    tmp_path, monkeypatch
):
    original = build_runtime(tmp_path)
    original.executor = GuardedSQLiteReadOnlyExecutor(
        original.executor.database_path, timeout_seconds=20
    )
    entered_sql, exited_sql, closed_sql = Event(), Event(), Event()
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            closed_sql.set()

    def observed_connect(database, *args, **kwargs):
        if database == f"file:{original.executor.database_path.as_posix()}?mode=ro":
            kwargs["factory"] = ObservedConnection
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    execute = original.executor.execute
    in_sql = [False]
    checks = [0]
    with assembly(tmp_path, original) as (service, _):
        check = service.queue.check

        def observed(claim):
            check(claim)
            if in_sql[0]:
                checks[0] += 1
                if checks[0] >= 2:  # execute pre-check, then actual SQLite progress callback.
                    entered_sql.set()

        service.queue.check = observed

        def expensive(*args, **kwargs):
            in_sql[0] = True
            try:
                return execute(LONG_SQL)
            finally:
                exited_sql.set()

        original.executor.execute = expensive
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "actual-sql")
        with ThreadPoolExecutor(1) as pool:
            worker = pool.submit(service.run_once)
            assert entered_sql.wait(5)
            started = time.monotonic()
            assert service.cancel(record.task_id, actor(), token).status == "cancel_requested"
            stopped = worker.result(timeout=5)
            assert exited_sql.is_set() and closed_sql.is_set()
            assert stopped.status == "cancelled"
            assert time.monotonic() - started < 5  # Not the 20-second SQL deadline.
        with pytest.raises(TaskQueueConflict):
            service.result(record.task_id, actor(), token)
        with pytest.raises(TaskQueueConflict):
            service.retry(record.task_id, actor(), token)


def test_sql_guard_is_scoped_and_does_not_poison_other_requests(tmp_path):
    path = tmp_path / "read.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fixture (value INTEGER)")
    executor = GuardedSQLiteReadOnlyExecutor(path)

    def denied():
        raise TaskCancellationRequested("cancel")

    with pytest.raises(TaskCancellationRequested), executor.guarded(denied):
        executor.execute("SELECT 1")
    assert executor.execute("SELECT 2").rows == ((2,),)
    with executor.guarded(lambda: None), ThreadPoolExecutor(1) as pool:
        assert pool.submit(executor.execute, "SELECT 3").result().rows == ((3,),)


def downgrade_fixture_to_v1(path):
    """Synthetic old on-disk layout; encrypted request format is unchanged."""
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE metric_jobs DROP COLUMN cancel_requested_at")
        db.execute("ALTER TABLE metric_jobs DROP COLUMN cancelled_at")
        db.execute("PRAGMA user_version = 1")


def test_explicit_offline_migration_preserves_encrypted_requests_and_fences_readers(tmp_path):
    queue = local_queue(tmp_path)
    record = accepted(queue)
    with sqlite3.connect(queue.path) as db:
        encrypted = db.execute("SELECT body FROM metric_jobs").fetchone()[0]
    downgrade_fixture_to_v1(queue.path)
    with pytest.raises(ValueError, match="offline_migration"):
        local_queue(tmp_path)
    with pytest.raises(ValueError, match="unsupported_task_queue_schema"):
        queue.read(record.task_id, actor())  # Existing reader checks every transaction.
    migrated = local_queue(tmp_path, migrate_v1_offline=True)
    assert migrated.read(record.task_id, actor()) == record
    with sqlite3.connect(queue.path) as db:
        assert db.execute("SELECT body FROM metric_jobs").fetchone()[0] == encrypted
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    # Historical v1 constructors admit only 0/1, so v2 is a hard cutover, not ignored fields.
    assert migrated.cancel(record.task_id, actor()).status == "cancelled"
    with pytest.raises(ValueError, match="v1_migration_required"):
        local_queue(tmp_path, migrate_v1_offline=True)


@pytest.mark.parametrize(
    "fault", ["running", "expired_running", "wrong_key", "corruption", "layout"]
)
def test_migration_refuses_unsafe_inputs_and_rolls_back(tmp_path, fault):
    queue = local_queue(tmp_path)
    accepted(queue)
    if fault in {"running", "expired_running"}:
        queue.claim()
    downgrade_fixture_to_v1(queue.path)
    with sqlite3.connect(queue.path) as db:
        if fault == "expired_running":
            db.execute("UPDATE metric_jobs SET lease_until = 0")
        if fault == "corruption":
            db.execute("UPDATE metric_jobs SET body = X'00'")
        if fault == "layout":
            db.execute("ALTER TABLE metric_jobs ADD COLUMN unknown TEXT")
    with pytest.raises((ValueError, PermissionError)):
        MetricTaskQueue(
            queue.path,
            encryption_key=(b"z" if fault == "wrong_key" else b"q") * 32,
            migrate_v1_offline=True,
        )
    with sqlite3.connect(queue.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "cancelled_at" not in [
            row[1] for row in db.execute("PRAGMA table_info(metric_jobs)")
        ]


def test_v2_contract_refuses_old_version_and_false_stop_metadata(tmp_path):
    record = accepted(local_queue(tmp_path))
    for update in (
        {"schema_version": "1.0"},
        {"status": "cancelled"},
        {"status": "cancel_requested"},
        {"cancelled_at": datetime.now(UTC)},
    ):
        with pytest.raises(ValueError):
            MetricTaskRecord.model_validate(record.model_dump() | update)


def test_pending_cancellation_is_not_a_terminal_sse_state(tmp_path):
    with assembly(tmp_path, build_runtime(tmp_path)) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "pending-stream")
        claim = service.queue.claim()
        service.cancel(record.task_id, actor(), token)
        prepared = MetricTaskStreams(service).prepare(record.task_id, actor(), token)

        async def consume():
            iterator = prepared.iterate(connected)
            first = await anext(iterator)
            assert parse_frames(first)[0]["data"]["status"] == "cancel_requested"
            service.queue.settle(claim, "interrupted")
            tail = "".join([frame async for frame in iterator])
            assert '"status":"cancelled"' in tail
            assert parse_frames(tail)[-1]["data"]["reason"] == "terminal"

        asyncio.run(consume())


def test_migration_ddl_failure_rolls_back_first_added_column(tmp_path, monkeypatch):
    queue = local_queue(tmp_path)
    accepted(queue)
    downgrade_fixture_to_v1(queue.path)
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "ALTER TABLE metric_jobs ADD COLUMN cancelled_at REAL":
                raise sqlite3.OperationalError("synthetic migration interruption")
            return super().execute(sql, *args, **kwargs)

    def faulty(*args, **kwargs):
        return connect(*args, **kwargs, factory=FailingConnection)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", faulty)
        with pytest.raises(sqlite3.OperationalError):
            local_queue(tmp_path, migrate_v1_offline=True)
    with connect(queue.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert len(list(db.execute("PRAGMA table_info(metric_jobs)"))) == 11
    assert local_queue(tmp_path, migrate_v1_offline=True).claim() is not None


def test_mislabeled_v2_layout_and_future_versions_fail_closed(tmp_path):
    queue = local_queue(tmp_path)
    accepted(queue)
    downgrade_fixture_to_v1(queue.path)
    for version in (2, 3):
        with sqlite3.connect(queue.path) as db:
            db.execute(f"PRAGMA user_version = {version}")
        with pytest.raises(ValueError, match="layout_unavailable|unsupported_task_queue_schema"):
            local_queue(tmp_path)


@pytest.mark.parametrize(
    "sql,limits",
    [
        ("DELETE FROM fixture", {}),
        ("SELECT 1 UNION ALL SELECT 2", {"max_rows": 1}),
        ("SELECT 1, 2", {"max_cells": 1}),
        ("SELECT 1 UNION ALL SELECT 2", {"max_cells": 1}),
        (LONG_SQL, {"timeout_seconds": 0.001}),
    ],
)
def test_guarded_adapter_retains_readonly_row_cell_and_deadline_limits(tmp_path, sql, limits):
    path = tmp_path / "read.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fixture (value INTEGER)")
    executor = GuardedSQLiteReadOnlyExecutor(path, **limits)
    with executor.guarded(lambda: None), pytest.raises(QueryExecutionError):
        executor.execute(sql)
