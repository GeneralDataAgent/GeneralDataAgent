import copy
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import FakeNarrator, actor, build_runtime, query
from test_stage11_task_submission import assembly, credentials

from generaldataagent.api.app import create_app
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.orchestration.task_queue import MetricTaskQueue, TaskQueueConflict
from generaldataagent.orchestration.task_service import MetricTaskExecution


def client_for(service):
    return TestClient(
        create_app(session_broker=service.broker, task_events=service.events, metric_tasks=service)
    )


def submit(service, token, key="result-1", owner=None):
    return service.submit(query(), owner or actor(), token, key)


def checkpoint_edit(service, saver, record, mutate):
    context = service.queue.result_context(record.task_id, actor())
    runtime = service.runtime_factory(MetricTaskExecution(context, lambda: None))
    config = {"configurable": {"thread_id": str(record.thread_id)}}
    with saver.execution(str(record.thread_id), actor()):
        state = copy.deepcopy(runtime.graph.get_state(config).values)
        mutate(state)
        runtime.graph.update_state(config, state, as_node="deliver")


def test_api_lists_then_reads_completed_result_without_reexecuting_or_charging(tmp_path):
    original = build_runtime(tmp_path)
    calls = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        calls.append("sql")
        return execute(*args, **kwargs)

    original.executor.execute = counted
    with assembly(tmp_path, original) as (service, saver):
        token, headers = credentials(service.broker)
        client = client_for(service)
        record = submit(service, token)
        url = f"/v1/analysis/tasks/{record.task_id}/result"
        assert client.get(url, headers=headers).status_code == 409
        listed = client.get("/v1/analysis/tasks", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["tasks"][0]["task_id"] == str(record.task_id)
        service.run_once()
        with sqlite3.connect(service.budgets.path) as db:
            budget_before = db.execute("SELECT * FROM task_budgets").fetchall()
        events_before = service.events.read(record.task_id, actor()).latest_sequence
        for _ in range(2):
            response = client.get(url, headers=headers)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            result = response.json()
            assert result["validation"]["status"] == "pass"
            assert result["task_id"] == str(record.task_id)
            assert result["thread_id"] == str(record.thread_id)
            assert result["delivery"]["rows"] == [["store", 1], ["web", 2]]
            assert "SELECT" not in response.text and "actor_context" not in response.text
            assert "session_reference" not in response.text
        assert calls == ["sql"]
        assert service.events.read(record.task_id, actor()).latest_sequence == events_before
        with sqlite3.connect(service.budgets.path) as db:
            assert db.execute("SELECT * FROM task_budgets").fetchall() == budget_before
        with saver.lock:
            assert saver.conn.execute("SELECT COUNT(*) FROM thread_executions").fetchone()[0] == 0


def test_result_uses_new_session_after_worker_grant_and_execution_budget_expire(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, clock=lambda: now) as (service, _):
        token, _ = credentials(service.broker)
        record = submit(service, token)
        assert service.run_once().status == "completed"
        now += timedelta(seconds=901)
        fresh, headers = credentials(service.broker)
        response = client_for(service).get(
            f"/v1/analysis/tasks/{record.task_id}/result", headers=headers
        )
        assert response.status_code == 200
        service.broker.revoke(fresh)
        assert (
            client_for(service)
            .get(f"/v1/analysis/tasks/{record.task_id}/result", headers=headers)
            .status_code
            == 401
        )


@pytest.mark.parametrize(
    "update",
    [
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("other",)},
    ],
)
def test_private_list_and_result_do_not_disclose_other_scopes(tmp_path, update):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        record = submit(service, token)
        service.run_once()
        stranger = ActorContext.model_validate(actor().model_dump() | update)
        _, headers = credentials(service.broker, stranger)
        client = client_for(service)
        assert client.get("/v1/analysis/tasks", headers=headers).json()["tasks"] == []
        inaccessible = client.get(f"/v1/analysis/tasks/{record.task_id}/result", headers=headers)
        unknown = client.get(f"/v1/analysis/tasks/{uuid4()}/result", headers=headers)
        assert inaccessible.status_code == unknown.status_code == 404
        assert inaccessible.json() == unknown.json()


def test_list_is_exact_domain_view_and_result_allows_covering_authorized_actor(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        record = submit(service, token)
        service.run_once()
        broad = actor().model_copy(update={"data_domains": ("commerce", "finance")})
        _, headers = credentials(service.broker, broad)
        pairs = list(headers.items()) + [("x-data-domain", "finance")]
        client = client_for(service)
        assert client.get("/v1/analysis/tasks", headers=pairs).json()["tasks"] == []
        assert (
            client.get(f"/v1/analysis/tasks/{record.task_id}/result", headers=pairs).status_code
            == 200
        )


def test_cursor_excludes_new_tasks_and_is_scope_bound_and_opaque(tmp_path):
    queue = MetricTaskQueue(tmp_path / "jobs.db", encryption_key=b"q" * 32)
    records = [queue.submit(query(), actor(), "a" * 64, f"item-{i}") for i in range(7)]
    page = queue.list_tasks(actor(), limit=2)
    cursor = page.next_cursor
    assert cursor and "user-1" not in cursor and "commerce" not in cursor
    new = queue.submit(query(), actor(), "a" * 64, "new")
    collected = list(page.tasks)
    while page.next_cursor:
        page = queue.list_tasks(actor(), limit=2, cursor=page.next_cursor)
        collected.extend(page.tasks)
    assert [r.task_id for r in collected] == [r.task_id for r in records]
    assert new.task_id not in {r.task_id for r in collected}
    other = actor().model_copy(update={"user_id": "other"})
    with pytest.raises(TaskQueueConflict):
        queue.list_tasks(other, cursor=cursor)
    changed = cursor[:-3] + ("AAA" if cursor[-3:] != "AAA" else "BBB")
    with pytest.raises(TaskQueueConflict):
        queue.list_tasks(actor(), cursor=changed)
    with pytest.raises(TaskQueueConflict):
        queue.list_tasks(actor(), cursor="invalid")


def test_cursor_anchor_deletion_and_rowid_reuse_require_restart(tmp_path):
    queue = MetricTaskQueue(tmp_path / "jobs.db", encryption_key=b"q" * 32)
    records = [queue.submit(query(), actor(), "a" * 64, str(i)) for i in range(3)]
    cursor = queue.list_tasks(actor(), limit=1).next_cursor
    with sqlite3.connect(queue.path) as db:
        db.execute("DELETE FROM metric_jobs WHERE task_id = ?", (str(records[-1].task_id),))
    queue.submit(query(), actor(), "a" * 64, "replacement")
    with pytest.raises(TaskQueueConflict):
        queue.list_tasks(actor(), cursor=cursor)


def test_cursor_expiry_bounds_and_new_token_reuse(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, clock=lambda: now) as (service, _):
        token, headers = credentials(service.broker)
        for i in range(3):
            submit(service, token, str(i))
        client = client_for(service)
        page = client.get("/v1/analysis/tasks", headers=headers, params={"limit": 1}).json()
        cursor = page["next_cursor"]
        service.broker.revoke(token)
        _, fresh = credentials(service.broker)
        assert (
            client.get("/v1/analysis/tasks", headers=fresh, params={"cursor": cursor}).status_code
            == 200
        )
        now += timedelta(seconds=300)
        assert (
            client.get("/v1/analysis/tasks", headers=fresh, params={"cursor": cursor}).status_code
            == 409
        )
        for value in (0, 101):
            assert (
                client.get("/v1/analysis/tasks", headers=fresh, params={"limit": value}).status_code
                == 422
            )
        assert (
            client.get(
                "/v1/analysis/tasks", headers=fresh, params={"cursor": "a" * 2049}
            ).status_code
            == 422
        )


def test_corrupt_other_user_payload_does_not_block_or_leak_into_list(tmp_path):
    queue = MetricTaskQueue(tmp_path / "jobs.db", encryption_key=b"q" * 32)
    own = queue.submit(query(), actor(), "a" * 64, "own")
    other = queue.submit(
        query(), actor().model_copy(update={"user_id": "other"}), "b" * 64, "other"
    )
    with sqlite3.connect(queue.path) as db:
        db.execute(
            "UPDATE metric_jobs SET body = ? WHERE task_id = ?", (b"broken", str(other.task_id))
        )
    assert [r.task_id for r in queue.list_tasks(actor()).tasks] == [own.task_id]


@pytest.mark.parametrize(
    "tamper", ["task", "actor", "query", "validation", "rows", "ref_task", "ref_uri"]
)
def test_result_rejects_inconsistent_checkpoint_or_evidence_binding(tmp_path, tamper):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, saver):
        token, headers = credentials(service.broker)
        record = submit(service, token)
        service.run_once()

        def change(state):
            if tamper == "task":
                state["task_id"] = str(uuid4())
            elif tamper == "actor":
                state["actor_context"]["user_id"] = "other"
            elif tamper == "query":
                state["metric_query"]["metric_version"] = "v2"
            elif tamper == "validation":
                state["validation_result"]["task_id"] = str(uuid4())
            elif tamper == "rows":
                for value in (state["analysis_result"], state["delivery_package"]):
                    value["rows"] = [["store", 999], ["web", 999]]
                    value["table"]["rows"] = [["store", 999], ["web", 999]]
            else:
                key = "task_id" if tamper == "ref_task" else "uri"
                value = (
                    str(uuid4())
                    if key == "task_id"
                    else f"artifact://local/{uuid4()}/{uuid4()}.json"
                )
                state["analysis_result"]["artifact"][key] = value
                state["delivery_package"]["evidence"][0][key] = value

        checkpoint_edit(service, saver, record, change)
        response = client_for(service).get(
            f"/v1/analysis/tasks/{record.task_id}/result", headers=headers
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "task_result_unavailable"}


@pytest.mark.parametrize("missing", [False, True])
def test_result_rechecks_artifact_bytes_and_never_reexecutes_missing_evidence(tmp_path, missing):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = submit(service, token)
        service.run_once()
        path = next((tmp_path / "artifacts" / str(record.task_id)).glob("*.json"))
        if missing:
            path.unlink()
        else:
            path.write_bytes(b"private-corruption-body")
        response = client_for(service).get(
            f"/v1/analysis/tasks/{record.task_id}/result", headers=headers
        )
        assert response.status_code == 409 and "private-corruption-body" not in response.text


def test_revocation_while_reading_evidence_prevents_result_return(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = submit(service, token)
        service.run_once()
        read = original.artifacts.read_bytes

        def revoke(ref):
            content = read(ref)
            service.broker.revoke(token)
            return content

        original.artifacts.read_bytes = revoke
        assert (
            client_for(service)
            .get(f"/v1/analysis/tasks/{record.task_id}/result", headers=headers)
            .status_code
            == 404
        )


def test_governed_result_honors_export_denial(tmp_path):
    original = build_runtime(tmp_path)

    class DeniedExport(GovernedArtifactStore):
        def export_bytes(self, ref):
            raise PermissionError("fixture export denied")

    original.artifacts = DeniedExport(
        tmp_path / "governed", index_path=tmp_path / "index.db", actor=actor()
    )
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = submit(service, token)
        assert service.run_once().status == "completed"
        response = client_for(service).get(
            f"/v1/analysis/tasks/{record.task_id}/result", headers=headers
        )
        assert response.status_code == 404


def test_disabled_endpoints_are_fail_closed():
    client = TestClient(create_app())
    assert client.get("/v1/analysis/tasks").status_code == 503
    assert client.get(f"/v1/analysis/tasks/{uuid4()}/result").status_code == 503


def test_result_restores_narrative_without_calling_model_again(tmp_path):
    original = build_runtime(tmp_path)
    calls = []

    class NarratedRuntime(MetricAnalysisRuntime):
        def __init__(self, **kwargs):
            class Narrator(FakeNarrator):
                def generate(self, evidence):
                    calls.append("model")
                    return super().generate(evidence)

            super().__init__(narrator=Narrator(), **kwargs)

    with assembly(tmp_path, original, runtime_type=NarratedRuntime) as (service, _):
        token, headers = credentials(service.broker)
        record = submit(service, token)
        assert service.run_once().status == "completed"
        response = client_for(service).get(
            f"/v1/analysis/tasks/{record.task_id}/result", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["delivery"]["narrative_model"] == "fake-model"
        assert calls == ["model"]
