import json

from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import COLUMNS, prepared
from test_stage13_xlsx_intake import build_service, client_for, declaration_body, declare
from test_stage13_xlsx_snapshot import snapshot


def profile(client, headers, file_id, snapshot_hash):
    return client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/profile",
        headers=headers,
        params={"snapshot_hash": snapshot_hash},
    )


def test_snapshot_profile_binds_canonical_counts_to_one_retained_snapshot(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers, columns=[COLUMNS[2], COLUMNS[0], COLUMNS[1]])
    current = snapshot(client, headers, file_id).json()
    before = (tmp_path / "xlsx.db").read_bytes()

    response = profile(client, headers, file_id, current["snapshot_hash"])
    assert response.status_code == 200, response.text
    report = response.json()
    for digest in ("source_hash", "confirmation_hash", "interpretation_hash", "snapshot_hash"):
        assert report[digest] == current[digest]
    assert report["file_id"] == file_id
    assert report["data_rows"] == 7
    assert report["duplicate_row_count"] == 1
    assert report["aggregation"] == "canonical-counts-only"
    assert [column["index"] for column in report["columns"]] == [0, 1, 2]
    counts = [
        (
            column["value_count"],
            column["distinct_count"],
            column["missing_count"],
            column["unparsed_count"],
            column["untrusted_count"],
        )
        for column in report["columns"]
    ]
    assert counts == [(6, 5, 1, 0, 0), (4, 3, 0, 2, 1), (6, 2, 0, 1, 0)]
    assert all("header" not in column and "values" not in column for column in report["columns"])
    assert profile(client, headers, file_id, current["snapshot_hash"]).json() == report
    assert (tmp_path / "xlsx.db").read_bytes() == before
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    reads = [event for event in events if event["event_type"] == "xlsx.snapshot_profile_read"]
    assert len(reads) == 2
    assert reads[0]["payload"]["snapshot_hash"] == current["snapshot_hash"]
    assert "alpha" not in json.dumps(reads)


def test_snapshot_profile_refuses_missing_foreign_stale_and_changed_versions(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, declared = prepared(client, headers)
    other_hash = "sha256:" + "0" * 64
    assert profile(client, headers, file_id, other_hash).json()["detail"] == (
        "xlsx_snapshot_not_derived"
    )
    current = snapshot(client, headers, file_id).json()["snapshot_hash"]
    foreign = actor().model_copy(update={"user_id": "another-creator"})
    _, foreign_headers = credentials(service.broker, foreign)
    assert profile(client, foreign_headers, file_id, current).status_code == 404
    assert profile(client, headers, file_id, "malformed").status_code == 422
    assert profile(client, headers, file_id, other_hash).json()["detail"] == "xlsx_snapshot_changed"

    revision = declaration_body(declared, columns=[COLUMNS[2]])
    assert declare(client, headers, file_id, revision).status_code == 200
    assert profile(client, headers, file_id, current).json()["detail"] == "xlsx_snapshot_stale"
    updated = snapshot(client, headers, file_id).json()["snapshot_hash"]
    assert profile(client, headers, file_id, current).json()["detail"] == "xlsx_snapshot_changed"
    assert profile(client, headers, file_id, updated).status_code == 200
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.snapshot_profile_read") == 1


def test_snapshot_profile_withholds_result_when_audit_fails(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    current = snapshot(client, headers, file_id).json()["snapshot_hash"]

    def fail(_event):
        raise OSError("audit unavailable")

    monkeypatch.setattr(service.audit, "emit", fail)
    response = profile(client, headers, file_id, current)
    assert response.status_code == 503
    assert response.json() == {"detail": "xlsx_intake_unavailable"}


def test_snapshot_profile_refuses_declaration_revised_during_audit(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, declared = prepared(client, headers)
    current = snapshot(client, headers, file_id).json()["snapshot_hash"]
    emit = service.audit.emit

    def revise(event):
        emit(event)
        if event.event_type == "xlsx.snapshot_profile_read":
            revision = declaration_body(declared, columns=[COLUMNS[2]])
            assert declare(client, headers, file_id, revision).status_code == 200

    monkeypatch.setattr(service.audit, "emit", revise)
    response = profile(client, headers, file_id, current)
    assert response.status_code == 409
    assert response.json() == {"detail": "xlsx_snapshot_stale"}
