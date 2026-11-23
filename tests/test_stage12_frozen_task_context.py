from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime, query
from test_stage11_task_submission import assembly, credentials
from test_stage12_business_context import confirmation, store

from generaldataagent.api.app import create_app
from generaldataagent.contracts import TaskContextBinding
from generaldataagent.orchestration.business_context import BusinessContextService


def binding(version=1):
    return TaskContextBinding(context_version=version)


def context_client(tmp_path, service):
    context = BusinessContextService(tasks=service, store=store(tmp_path))
    client = TestClient(
        create_app(
            session_broker=service.broker,
            task_events=service.events,
            metric_tasks=service,
            business_context=context,
        )
    )
    return client, context


def bound_payload(version=1):
    payload = query().model_dump(mode="json")
    payload["context_binding"] = binding(version).model_dump(mode="json")
    return payload


def submit_bound(client, headers, context_version=1, key="context-1"):
    changed = dict(headers)
    changed["idempotency-key"] = key
    return client.post(
        "/v1/analysis/tasks",
        headers=changed,
        json=bound_payload(context_version),
    )


def test_binding_is_pinned_at_acceptance_and_cited_without_runtime_injection(tmp_path):
    original = build_runtime(tmp_path)
    executed = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        executed.append("sql")
        return execute(*args, **kwargs)

    original.executor.execute = counted
    with assembly(tmp_path, original) as (service, _):
        client, context = context_client(tmp_path, service)
        token, headers = credentials(service.broker)
        assert context.confirm(actor(), token, confirmation()).version == 1
        submitted = submit_bound(client, headers)
        assert submitted.status_code == 202
        record = submitted.json()
        assert record["context_binding"] == binding().model_dump(mode="json")
        assert record["schema_version"] == "2.1"
        assert executed == []
        assert b"GMV" not in (tmp_path / "jobs.db").read_bytes()
        assert b"context_version" not in (tmp_path / "checkpoint.db").read_bytes()
        completed = service.run_once()
        assert completed.status == "completed"
        assert service.queue.result_context(completed.task_id, actor()).context_binding == binding()
        result = client.get(f"/v1/analysis/tasks/{completed.task_id}/result", headers=headers)
        assert result.status_code == 200
        assert result.json()["task_id"] == str(completed.task_id)


def test_stale_revoked_and_unconfigured_bindings_fail_closed(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        client, context = context_client(tmp_path, service)
        token, headers = credentials(service.broker)
        assert context.confirm(actor(), token, confirmation()).version == 1
        stale = submit_bound(client, headers, context_version=2)
        assert stale.status_code == 403 and stale.json()["detail"] == "task_submission_denied"
        unbound = client.post(
            "/v1/analysis/tasks", headers=headers, json=query().model_dump(mode="json")
        )
        assert unbound.status_code == 202
        assert unbound.json()["context_binding"] is None
        first = submit_bound(client, headers)
        assert first.status_code == 202
        record_id = first.json()["task_id"]
        assert service.run_once().task_id != __import__("uuid").UUID(record_id)
        assert service.run_once().task_id == __import__("uuid").UUID(record_id)
        context.revoke(actor(), token, 1)
        result = client.get(f"/v1/analysis/tasks/{record_id}/result", headers=headers)
        assert result.status_code == 404 and result.json()["detail"] == "task_result_unavailable"


def test_binding_is_idempotency_input_and_requires_strict_contract(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        client, context = context_client(tmp_path, service)
        token, headers = credentials(service.broker)
        assert context.confirm(actor(), token, confirmation()).version == 1
        first = submit_bound(client, headers, key="same-key")
        assert first.status_code == 202
        changed = dict(headers)
        changed["idempotency-key"] = "same-key"
        repeat = client.post(
            "/v1/analysis/tasks",
            headers=changed,
            json=query().model_dump(mode="json"),
        )
        assert repeat.status_code == 409
        assert repeat.json()["detail"] == "task_idempotency_request_changed"
        payload = bound_payload()
        payload["context_binding"] = {"context_version": True}
        invalid = client.post("/v1/analysis/tasks", headers=headers, json=payload)
        assert invalid.status_code == 422
        service.business_context = None
        denied = submit_bound(client, headers, key="without-port")
        assert denied.status_code == 403
