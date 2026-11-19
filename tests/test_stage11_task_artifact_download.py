import hashlib
import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime
from test_stage11_task_results import client_for, submit
from test_stage11_task_submission import assembly, credentials

from generaldataagent.api.app import create_app


def completed_result(service, token):
    record = submit(service, token)
    assert service.run_once().status == "completed"
    return record, service.result(record.task_id, actor(), token)


def artifact_url(task_id, artifact_id):
    return f"/v1/analysis/tasks/{task_id}/artifacts/{artifact_id}"


def test_downloads_each_delivery_evidence_artifact_without_reexecuting_or_charging(tmp_path):
    original = build_runtime(tmp_path)
    calls = []
    execute = original.executor.execute

    def counted(*args, **kwargs):
        calls.append("sql")
        return execute(*args, **kwargs)

    original.executor.execute = counted
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record, result = completed_result(service, token)
        client = client_for(service)
        with sqlite3.connect(service.budgets.path) as db:
            budget_before = db.execute("SELECT * FROM task_budgets").fetchall()

        for ref in result.delivery.evidence:
            response = client.get(artifact_url(record.task_id, ref.artifact_id), headers=headers)
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/json"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-disposition"] == (
                f'attachment; filename="{ref.artifact_type}-{ref.artifact_id}.json"'
            )
            assert len(response.content) == ref.size_bytes
            assert f"sha256:{hashlib.sha256(response.content).hexdigest()}" == ref.content_hash
            assert json.loads(response.content)

        assert calls == ["sql"]
        with sqlite3.connect(service.budgets.path) as db:
            assert db.execute("SELECT * FROM task_budgets").fetchall() == budget_before

        events = [
            json.loads(line)
            for line in (tmp_path / "audit" / "events.jsonl").read_text().splitlines()
        ]
        downloads = [event for event in events if event["event_type"] == "artifact.downloaded"]
        assert len(downloads) == len(result.delivery.evidence)
        assert {event["payload"]["artifact_type"] for event in downloads} == {
            ref.artifact_type for ref in result.delivery.evidence
        }
        assert all(set(event["payload"]) == {"artifact_type", "size_bytes"} for event in downloads)


def test_unknown_foreign_non_evidence_and_cross_domain_artifacts_are_indistinguishable(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record, result = completed_result(service, token)
        evidence_id = result.delivery.evidence[0].artifact_id
        unreferenced = original.artifacts.put_json(
            task_id=record.task_id,
            artifact_type="unreferenced_private_body",
            value={"private": "body"},
            created_by=actor().user_id,
        )
        client = client_for(service)
        _, other_user = credentials(service.broker, actor().model_copy(update={"user_id": "other"}))
        _, other_domain = credentials(
            service.broker, actor().model_copy(update={"data_domains": ("finance",)})
        )
        responses = [
            client.get(artifact_url(record.task_id, uuid4()), headers=headers),
            client.get(artifact_url(record.task_id, unreferenced.artifact_id), headers=headers),
            client.get(artifact_url(record.task_id, evidence_id), headers=other_user),
            client.get(artifact_url(record.task_id, evidence_id), headers=other_domain),
        ]
        assert {response.status_code for response in responses} == {404}
        assert {response.json()["detail"] for response in responses} == {
            "task_artifact_unavailable"
        }
        assert all("private" not in response.text for response in responses)


def test_incomplete_task_and_tampered_evidence_deny_download(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        queued = submit(service, token)
        client = client_for(service)
        denied = client.get(artifact_url(queued.task_id, uuid4()), headers=headers)
        assert denied.status_code == 409
        assert denied.json() == {"detail": "task_artifact_unavailable"}

        assert service.run_once().status == "completed"
        result = service.result(queued.task_id, actor(), token)
        path = next((tmp_path / "artifacts" / str(queued.task_id)).glob("*.json"))
        path.write_bytes(b"private-corruption-body")
        denied = client.get(
            artifact_url(queued.task_id, result.delivery.evidence[0].artifact_id),
            headers=headers,
        )
        assert denied.status_code == 409
        assert "private-corruption-body" not in denied.text


def test_revocation_while_reading_evidence_prevents_download(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record, result = completed_result(service, token)
        read = original.artifacts.read_bytes

        def revoke(ref):
            content = read(ref)
            service.broker.revoke(token)
            return content

        original.artifacts.read_bytes = revoke
        response = client_for(service).get(
            artifact_url(record.task_id, result.delivery.evidence[0].artifact_id),
            headers=headers,
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "task_artifact_unavailable"}


def test_missing_or_failing_audit_denies_download(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record, result = completed_result(service, token)
        client = client_for(service)
        url = artifact_url(record.task_id, result.delivery.evidence[0].artifact_id)

        def fail(event):
            raise RuntimeError("audit unavailable")

        original.audit.emit = fail
        assert client.get(url, headers=headers).status_code == 409

        original.audit = None
        assert client.get(url, headers=headers).status_code == 409


def test_artifact_download_is_fail_closed_when_not_configured():
    client = TestClient(create_app())
    response = client.get(f"/v1/analysis/tasks/{uuid4()}/artifacts/{uuid4()}")
    assert response.status_code == 503
    assert response.json() == {"detail": "metric_tasks_not_configured"}
