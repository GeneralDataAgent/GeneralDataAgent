import hashlib
import json
import os

import pytest
from test_metric_analysis import actor, build_runtime, query
from test_stage11_task_results import checkpoint_edit, client_for
from test_stage11_task_submission import assembly, credentials
from test_stage12_business_context import confirmation, store
from test_stage12_frozen_task_context import binding, context_client, submit_bound
from test_stage16_question_tasks import question_body

from generaldataagent.api.app import create_app
from generaldataagent.orchestration.business_context import BusinessContextService
from generaldataagent.orchestration.task_service import MetricTaskExecution


def test_api_rejects_a_context_store_different_from_the_task_binding_store(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        context_client(tmp_path, service)
        other = tmp_path / "other"
        other.mkdir()
        alternate = BusinessContextService(tasks=service, store=store(other))
        with pytest.raises(ValueError, match="business_context_must_share_task_service"):
            create_app(
                session_broker=service.broker,
                task_events=service.events,
                metric_tasks=service,
                business_context=alternate,
            )


@pytest.mark.parametrize("before,after", [(1, None), (None, 1), (1, 2)])
def test_question_idempotency_covers_the_frozen_binding(tmp_path, before, after):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, question_catalog=original.catalog) as (service, _):
        client, context = context_client(tmp_path, service)
        token, headers = credentials(service.broker)
        context.confirm(actor(), token, confirmation())
        headers = {**headers, "idempotency-key": "question-binding"}

        def body(version):
            return question_body() | (
                {} if version is None else {"context_binding": binding(version).model_dump()}
            )

        first = client.post("/v1/analysis/tasks/questions", headers=headers, json=body(before))
        assert first.status_code == 202
        replay = client.post("/v1/analysis/tasks/questions", headers=headers, json=body(before))
        assert replay.status_code == 202 and replay.json() == first.json()
        if after == 2:
            context.confirm(actor(), token, confirmation(1))
        changed = client.post("/v1/analysis/tasks/questions", headers=headers, json=body(after))
        assert changed.status_code == 409
        assert changed.json() == {"detail": "task_idempotency_request_changed"}


def test_binding_never_enters_the_executed_checkpoint_query(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, saver):
        client, context = context_client(tmp_path, service)
        token, headers = credentials(service.broker)
        context.confirm(actor(), token, confirmation())
        assert submit_bound(client, headers).status_code == 202
        record = service.run_once()
        assert record.status == "completed"
        context = service.queue.result_context(record.task_id, actor())
        runtime = service.runtime_factory(MetricTaskExecution(context, lambda: None))
        with saver.execution(str(record.thread_id), actor()):
            state = runtime.graph.get_state({"configurable": {"thread_id": str(record.thread_id)}})
        assert "context_binding" not in state.values["metric_query"]
        assert context.context_binding == binding()


def test_pre_binding_queue_payload_still_reads_and_replays(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "legacy")
        queue = service.queue
        # Seed the encrypted v2 wire format published before TaskContextBinding existed.
        with queue._db(write=True) as db:
            row = dict(db.execute("SELECT * FROM metric_jobs").fetchone())
            raw = row["body"]
            payload = json.loads(queue.cipher.decrypt(raw[:12], raw[12:], queue._aad(row)))
            payload.pop("context_binding", None)
            payload["query"].pop("context_binding", None)
            row["request_hash"] = hashlib.sha256(
                json.dumps(payload["query"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            nonce = os.urandom(12)
            sealed = nonce + queue.cipher.encrypt(
                nonce, json.dumps(payload).encode(), queue._aad(row)
            )
            db.execute(
                "UPDATE metric_jobs SET request_hash=?, body=?", (row["request_hash"], sealed)
            )
        assert service.read(record.task_id, actor(), token).task_id == record.task_id
        assert service.submit(query(), actor(), token, "legacy").task_id == record.task_id
        assert service.run_once().status == "completed"


def test_pre_binding_checkpoint_result_remains_readable(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, saver):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "legacy-result")
        assert service.run_once().status == "completed"
        checkpoint_edit(
            service, saver, record, lambda state: state["metric_query"].pop("context_binding", None)
        )
        result = client_for(service).get(
            f"/v1/analysis/tasks/{record.task_id}/result", headers=headers
        )
        assert result.status_code == 200
