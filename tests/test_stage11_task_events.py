import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_metric_analysis import actor, build_runtime, query

from generaldataagent.api.app import create_app
from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.task_events import TaskEventJournal
from generaldataagent.security.identity import SessionBroker


def authenticated_headers(broker, owner=None, permissions=frozenset({"analysis:run"})):
    owner = owner or actor()
    token = broker.issue(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        data_domains=frozenset(owner.data_domains),
        permissions=permissions,
    )
    return {
        "Authorization": f"Bearer {token}",
        "x-user-id": owner.user_id,
        "x-tenant-id": owner.tenant_id,
        "x-project-id": owner.project_id,
        "x-data-domain": owner.data_domains[0],
    }


def test_real_metric_path_emits_only_metadata_and_api_replays_pages(tmp_path):
    runtime = build_runtime(tmp_path)
    journal = TaskEventJournal(tmp_path / "events.db")
    runtime.task_events = journal
    broker = SessionBroker()
    client = TestClient(create_app(runtime, session_broker=broker, task_events=journal))
    headers = authenticated_headers(broker)
    response = client.post(
        "/v1/analysis/metrics", headers=headers, json=query().model_dump(mode="json")
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert response.json()["status"] == "completed"
    url = f"/v1/analysis/tasks/{task_id}/events"
    collected = []
    cursor = 0
    while True:
        response = client.get(url, headers=headers, params={"after": cursor, "limit": 2})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        collected.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [e["sequence"] for e in collected] == list(range(1, 8))
    assert [e["phase"] for e in collected[1:-1]] == [
        "discover",
        "plan",
        "analyze",
        "validate",
        "deliver",
    ]
    assert collected[-1]["outcome"] == "completed"
    assert page["terminal"] is True
    assert "orders" not in str(collected)
    assert "SELECT" not in str(collected)
    assert client.get(url, headers=headers, params={"after": cursor}).json()["events"] == []
    reopened = TaskEventJournal(tmp_path / "events.db")
    assert len(reopened.read(task_id, actor()).events) == 7


@pytest.mark.parametrize(
    "update",
    [
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("other",)},
    ],
)
def test_store_and_http_reject_scope_changes_without_disclosing_existence(tmp_path, update):
    journal = TaskEventJournal(tmp_path / "events.db")
    task_id = uuid4()
    journal.start(task_id, actor())
    stranger = ActorContext.model_validate(actor().model_dump() | update)
    with pytest.raises(PermissionError, match="unavailable"):
        journal.read(task_id, stranger)
    with pytest.raises(PermissionError):
        journal.append(task_id, stranger, "interrupted")
    broker = SessionBroker()
    client = TestClient(create_app(session_broker=broker, task_events=journal))
    headers = authenticated_headers(broker, stranger)
    existing = client.get(f"/v1/analysis/tasks/{task_id}/events", headers=headers)
    missing = client.get(f"/v1/analysis/tasks/{uuid4()}/events", headers=headers)
    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json()


def test_http_checks_auth_permission_headers_and_disabled_service(tmp_path):
    journal = TaskEventJournal(tmp_path / "events.db")
    task_id = uuid4()
    journal.start(task_id, actor())
    broker = SessionBroker()
    client = TestClient(create_app(session_broker=broker, task_events=journal))
    url = f"/v1/analysis/tasks/{task_id}/events"
    headers = authenticated_headers(broker)
    assert client.get(url).status_code == 401
    denied = authenticated_headers(broker, permissions=frozenset())
    assert client.get(url, headers=denied).status_code == 403
    assert client.get(url, headers=headers | {"x-user-id": "forged"}).status_code == 403
    for params in ({"after": -1}, {"limit": 0}, {"limit": 201}):
        assert client.get(url, headers=headers, params=params).status_code == 422
    assert client.get(url, headers=headers, params={"after": 99}).status_code == 409
    disabled = TestClient(
        create_app(session_broker=broker, task_events=journal, service_enabled=lambda: False)
    )
    assert disabled.get(url, headers=headers).status_code == 503
    broker.revoke(headers["Authorization"].split()[1])
    assert client.get(url, headers=headers).status_code == 401
    assert TestClient(create_app()).get(url).status_code == 503


def test_requested_domains_cannot_hide_required_task_domain(tmp_path):
    journal = TaskEventJournal(tmp_path / "events.db")
    owner = actor().model_copy(update={"data_domains": ("commerce", "finance")})
    task_id = uuid4()
    journal.start(task_id, owner)
    broker = SessionBroker()
    headers = authenticated_headers(broker, owner)
    client = TestClient(create_app(session_broker=broker, task_events=journal))
    url = f"/v1/analysis/tasks/{task_id}/events"
    assert client.get(url, headers=headers).status_code == 404
    pairs = list(headers.items()) + [("x-data-domain", "finance")]
    assert client.get(url, headers=pairs).status_code == 200


def test_cross_connection_concurrent_appends_have_no_lost_or_duplicate_sequences(tmp_path):
    path = tmp_path / "events.db"
    journal = TaskEventJournal(path)
    other = TaskEventJournal(path)
    task_id = uuid4()
    journal.start(task_id, actor())
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(
            pool.map(
                lambda index: (journal if index % 2 else other).append(
                    task_id, actor(), "phase", phase="analyze"
                ),
                range(40),
            )
        )
    assert sorted(e.sequence for e in events) == list(range(2, 42))
    assert journal.read(task_id, actor()).latest_sequence == 41
    journal.append(task_id, actor(), "finished", outcome="failed")
    with pytest.raises(ValueError, match="terminal"):
        other.append(task_id, actor(), "interrupted")
    with pytest.raises(ValueError, match="already_exists"):
        other.start(task_id, actor())


def test_expiry_is_enforced_before_physical_cleanup_and_clock_must_be_aware(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    journal = TaskEventJournal(tmp_path / "events.db", clock=lambda: now, retention_seconds=10)
    task_id = uuid4()
    journal.start(task_id, actor())
    now += timedelta(seconds=10)
    with pytest.raises(PermissionError):
        journal.read(task_id, actor())
    with pytest.raises(PermissionError):
        journal.append(task_id, actor(), "interrupted")
    assert journal.purge_expired() == 1
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    now = datetime(2026, 11, 2)
    with pytest.raises(ValueError, match="aware"):
        journal.start(uuid4(), actor())


def test_failure_emits_interruption_without_exception_text_and_stops_delivery(tmp_path):
    runtime = build_runtime(tmp_path)
    journal = TaskEventJournal(tmp_path / "events.db")
    runtime.task_events = journal

    def fail(*args, **kwargs):
        raise RuntimeError("secret-query-and-credentials")

    runtime.executor.execute = fail
    with pytest.raises(RuntimeError, match="secret-query"):
        runtime.run(query(), actor())
    with sqlite3.connect(journal.path) as db:
        task_id = db.execute("SELECT task_id FROM event_tasks").fetchone()[0]
    page = journal.read(task_id, actor())
    assert page.events[-1].kind == "interrupted"
    assert page.terminal is False
    assert "secret-query" not in page.model_dump_json()
    assert "deliver" not in [e.phase for e in page.events]


def test_bad_metadata_rolls_back_and_budget_stops_before_next_handler(tmp_path):
    journal = TaskEventJournal(tmp_path / "events.db", max_events_per_task=2)
    task_id = uuid4()
    journal.start(task_id, actor())
    with pytest.raises(ValidationError):
        journal.append(task_id, actor(), "phase", phase="SELECT secret")
    assert journal.read(task_id, actor()).latest_sequence == 1
    journal.append(task_id, actor(), "interrupted")
    with pytest.raises(ValueError, match="budget"):
        journal.append(task_id, actor(), "interrupted")
    with pytest.raises(ValueError, match="cursor"):
        journal.read(task_id, actor(), after=True)


def test_deleted_event_cannot_be_silently_skipped(tmp_path):
    journal = TaskEventJournal(tmp_path / "events.db")
    task_id = uuid4()
    journal.start(task_id, actor())
    journal.append(task_id, actor(), "interrupted")
    with sqlite3.connect(journal.path) as db:
        db.execute("DELETE FROM task_events WHERE sequence = 1")
    with pytest.raises(ValueError, match="corrupt"):
        journal.read(task_id, actor())


def test_api_assembly_refuses_unqualified_or_disconnected_ports(tmp_path):
    journal = TaskEventJournal(tmp_path / "events.db")
    with pytest.raises(ValueError, match="authenticated"):
        create_app(task_events=journal)
    with pytest.raises(ValueError, match="production"):
        create_app(task_events=journal, session_broker=SessionBroker(), production_mode=True)
    with pytest.raises(ValueError, match="share_runtime"):
        create_app(build_runtime(tmp_path), task_events=journal, session_broker=SessionBroker())


def test_schema_and_configuration_fail_closed(tmp_path):
    for options in ({"retention_seconds": 0}, {"max_events_per_task": True}):
        with pytest.raises(ValueError):
            TaskEventJournal(tmp_path / "invalid.db", **options)
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 2")
    with pytest.raises(ValueError, match="unsupported"):
        TaskEventJournal(path)
    path = tmp_path / "unrelated.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(ValueError, match="dedicated"):
        TaskEventJournal(path)


def test_restart_resumes_failed_delivery_without_repeating_completed_sql(tmp_path):
    original = build_runtime(tmp_path)
    journal_path, saver_path = tmp_path / "events.db", tmp_path / "checkpoints.db"
    journal = TaskEventJournal(journal_path)
    calls = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        calls.append("sql")
        return execute(*args, **kwargs)

    class FailingDeliveryRuntime(MetricAnalysisRuntime):
        def _deliver(self, state):
            raise RuntimeError("temporary delivery outage")

    original.executor.execute = counted
    thread_id = str(uuid4())

    def configured(saver, events, runtime_type=MetricAnalysisRuntime):
        return runtime_type(
            catalog=original.catalog,
            executor=original.executor,
            artifacts=original.artifacts,
            audit=original.audit,
            checkpointer=saver,
            task_events=events,
        )

    with open_protected_checkpointer(saver_path, encryption_key=b"k" * 32) as saver:
        runtime = configured(saver, journal, FailingDeliveryRuntime)
        with pytest.raises(RuntimeError, match="delivery outage"):
            runtime.run(query(), actor(), thread_id=thread_id)
        task_id = runtime.graph.get_state({"configurable": {"thread_id": thread_id}}).values[
            "task_id"
        ]
        assert journal.read(task_id, actor()).events[-1].kind == "interrupted"
    with open_protected_checkpointer(saver_path, encryption_key=b"k" * 32) as saver:
        reopened = TaskEventJournal(journal_path)
        runtime = configured(saver, reopened)
        result = runtime.resume(thread_id, actor())
        assert result["status"] == "completed"
        page = reopened.read(task_id, actor())
        assert page.terminal
        assert page.latest_sequence == 9
        assert [e.phase for e in page.events].count("deliver") == 2
        assert calls == ["sql"]
        assert runtime.resume(thread_id, actor())["status"] == "completed"
        assert reopened.read(task_id, actor()).latest_sequence == 9


def test_journal_failure_stops_before_query_and_terminal_gap_can_be_reconciled(tmp_path):
    original = build_runtime(tmp_path)

    class UnavailableJournal(TaskEventJournal):
        def append(self, *args, **kwargs):
            raise OSError("journal offline")

    journal = UnavailableJournal(tmp_path / "offline.db")
    original.task_events = journal
    with pytest.raises(OSError, match="journal offline"):
        original.run(query(), actor())
    assert not list((tmp_path / "artifacts").rglob("*.json"))

    class TerminalOutage(TaskEventJournal):
        def append(self, task_id, owner, kind, **metadata):
            if kind == "finished":
                raise OSError("terminal event offline")
            return super().append(task_id, owner, kind, **metadata)

    path = tmp_path / "terminal.db"
    thread_id = str(uuid4())
    with open_protected_checkpointer(tmp_path / "checkpoint", encryption_key=b"k" * 32) as saver:
        runtime = MetricAnalysisRuntime(
            catalog=original.catalog,
            executor=original.executor,
            artifacts=original.artifacts,
            checkpointer=saver,
            task_events=TerminalOutage(path),
        )
        with pytest.raises(OSError, match="terminal event offline"):
            runtime.run(query(), actor(), thread_id=thread_id)
        runtime.task_events = TaskEventJournal(path)
        result = runtime.resume(thread_id, actor())
        assert result["status"] == "completed"
        assert runtime.task_events.read(result["task_id"], actor()).terminal


@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_validation_failure_records_terminal_outcome_without_delivery(tmp_path, status):
    original = build_runtime(tmp_path)
    journal = TaskEventJournal(tmp_path / "events.db")

    class RefusingValidationRuntime(MetricAnalysisRuntime):
        def _validate(self, state):
            return {"validation_result": {"status": status}}

        def _deliver(self, state):
            raise AssertionError("failed validation cannot deliver")

    runtime = RefusingValidationRuntime(
        catalog=original.catalog,
        executor=original.executor,
        artifacts=original.artifacts,
        task_events=journal,
    )
    result = runtime.run(query(), actor())
    assert result["status"] == status
    page = journal.read(result["task_id"], actor())
    assert page.terminal
    assert page.events[-1].outcome == status
    assert "deliver" not in [e.phase for e in page.events]
