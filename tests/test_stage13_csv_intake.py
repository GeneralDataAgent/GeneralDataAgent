import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor
from test_stage11_task_submission import credentials

from generaldataagent.api.app import create_app
from generaldataagent.connectors.base import ConnectorError
from generaldataagent.data_engineering import DataEngineeringRuntime
from generaldataagent.datasets.csv_intake import CsvIntakeService
from generaldataagent.datasets.csv_intake_contracts import ConfirmCsvText
from generaldataagent.datasets.csv_intake_store import (
    CsvIntakeConflict,
    CsvIntakeUnavailable,
    CsvUploadStore,
)
from generaldataagent.datasets.csv_parser import (
    CsvIntakeError,
    CsvIntakeLimit,
    CsvLimits,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.identity import SQLiteSessionBroker

RAW = b"id,amount,note\r\n001,10,\r\n002,2,=SUM(1)\r\n002,2,=SUM(1)\r\n"


def build_service(
    tmp_path, *, limits=None, clock=None, scanner=None, retention=86400, max_files=20, max_total=200
):
    broker = SQLiteSessionBroker(tmp_path / "sessions.db", clock=clock)
    store = CsvUploadStore(
        tmp_path / "csv.db",
        encryption_key=b"i" * 32,
        clock=clock,
        max_files_per_actor=max_files,
        max_total_files=max_total,
    )
    return CsvIntakeService(
        store=store,
        broker=broker,
        budgets=TaskBudgetLedger(tmp_path / "budgets.db", clock=clock),
        artifact_root=tmp_path / "content",
        artifact_index=tmp_path / "artifact-index.db",
        scratch_root=tmp_path / "scratch",
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        limits=limits,
        scanner=scanner,
        retention_seconds=retention,
    )


def client_for(service, **kwargs):
    return TestClient(create_app(session_broker=service.broker, csv_intake=service, **kwargs))


def confirmation(preview):
    record = preview.file if hasattr(preview, "file") else preview["file"]
    fields = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
    return ConfirmCsvText(
        confirmed=True, source_hash=fields["source_hash"], preview_hash=fields["preview_hash"]
    )


def test_actual_api_upload_confirm_profile_preserves_text_and_private_provenance(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    client = client_for(service)
    uploaded = client.post(
        "/v1/analysis/files/csv", headers=headers | {"content-type": "text/csv"}, content=RAW
    )
    assert uploaded.status_code == 201, uploaded.text
    preview = uploaded.json()
    file_id = preview["file"]["file_id"]
    assert preview["file"]["source_hash"] == "sha256:" + hashlib.sha256(RAW).hexdigest()
    assert preview["file"]["status"] == "staged"
    assert preview["data"]["row_count"] == 3
    assert preview["data"]["empty_cells"] == [0, 0, 1]
    assert preview["data"]["duplicate_rows"] == 1 and preview["data"]["formula_like_cells"] == 2
    assert preview["data"]["sample_rows"][0] == ["001", "10", ""]
    assert not list(service.artifact_root.rglob("*.json"))
    path = f"/v1/analysis/files/csv/{file_id}"
    assert client.get(path + "/profile", headers=headers).status_code == 409
    result = client.post(
        path + "/confirm", headers=headers, json=confirmation(preview).model_dump(mode="json")
    )
    assert result.status_code == 200, result.text
    report = result.json()
    assert report["file"]["status"] == "profiled"
    assert report["profile"]["row_count"] == 3
    amount = report["profile"]["columns"][1]
    assert amount["data_type"] == "TEXT" and amount["minimum"] == "10" and amount["maximum"] == "2"
    assert amount["mean"] is None and report["profile"]["columns"][2]["null_count"] == 0
    assert {r["artifact_type"] for r in report["evidence"]} == {
        "data_profile",
        "csv_profile_provenance",
    }
    assert client.get(path + "/profile", headers=headers).json() == report
    assert client.get(path, headers=headers).json()["file"]["status"] == "profiled"
    assert not list(service.scratch_root.iterdir())
    assert RAW not in service.store.path.read_bytes()
    assert token not in (tmp_path / "audit.jsonl").read_text()
    assert "=SUM" not in (tmp_path / "audit.jsonl").read_text()


def test_state_reopen_duplicate_submission_and_confirmation_never_reexecute_profile(
    tmp_path, monkeypatch
):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "same")
    first = service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    rebuilt = build_service(tmp_path)
    second_token, _ = credentials(rebuilt.broker)

    def no_profile(*args, **kwargs):
        raise AssertionError("completed intake must not re-profile")

    monkeypatch.setattr(DataEngineeringRuntime, "profile_relation", no_profile)
    assert (
        rebuilt.confirm(preview.file.file_id, actor(), second_token, confirmation(preview)) == first
    )
    assert rebuilt.result(preview.file.file_id, actor(), second_token) == first
    again = rebuilt.stage(RAW, actor(), second_token, "same")
    assert again.file.file_id == preview.file.file_id and again.file.status == "profiled"
    with pytest.raises(CsvIntakeConflict):
        rebuilt.stage(b"id\nother\n", actor(), second_token, "same")
    assert not list(rebuilt.scratch_root.iterdir())


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"a,b\n",
        b"a,a\n1,2\n",
        b"A,a\n1,2\n",
        b"a,\n1,2\n",
        b" a,b\n1,2\n",
        b"a,b\n1\n",
        b"a,b\n1,2,3\n",
        b'a\nx"y\n',
        b'a\n"unclosed\n',
        b'a\n"x"q\n',
        b"a\n\n",
        b"a\nx\x00\n",
        b"a\nx\x01\n",
        b"\xff\xfea",
        b"PK\x03\x04xx",
        b"\x1f\x8bxx",
        b"\xd0\xcf\x11\xe0xx",
        "Ａ,A\n1,2\n".encode(),
        "a\u200b\n1\n".encode(),
        b'"a\nb"\n1\n',
    ],
)
def test_malformed_or_ambiguous_csv_is_rejected_without_persisting(tmp_path, raw):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    with pytest.raises(CsvIntakeError):
        service.stage(raw, actor(), token, "bad")
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 0


def test_utf8_bom_quotes_multiline_empty_values_and_sql_like_identifiers_stay_text(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    raw = '\ufeff"客户","a""; DROP TABLE x;--"\r\n"华东","line1\r\nline2"\r\n"华北",""\r\n'.encode()
    preview = service.stage(raw, actor(), token, "unicode")
    assert preview.data.columns == ("客户", 'a"; DROP TABLE x;--')
    assert preview.data.sample_rows[0][1] == "line1\r\nline2"
    result = service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    assert result.profile.row_count == 2 and result.profile.columns[1].minimum == ""
    assert not list(service.scratch_root.iterdir())


@pytest.mark.parametrize(
    "raw",
    [
        b"password\nsynthetic_credential\n",
        b"name\nowner@example.com\n",
        b"name\nBearer abcdefghijklmnopqrstuvwxyz\n",
        b"phone\n13800138000\n",
    ],
)
def test_dlp_blocks_credentials_or_personal_data_before_staging(tmp_path, raw):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    with pytest.raises(PermissionError):
        service.stage(raw, actor(), token, "dlp")
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 0


@pytest.mark.parametrize(
    "limits,raw",
    [
        (CsvLimits(max_bytes=3), b"a\nx\n"),
        (CsvLimits(max_rows=1), b"a\nx\ny\n"),
        (CsvLimits(max_columns=1), b"a,b\nx,y\n"),
        (CsvLimits(max_cell_bytes=2), b"a\nxxx\n"),
    ],
)
def test_byte_row_column_and_cell_bounds_are_hard_failures(tmp_path, limits, raw):
    service = build_service(tmp_path, limits=limits)
    token, _ = credentials(service.broker)
    with pytest.raises(CsvIntakeLimit):
        service.stage(raw, actor(), token, "limit")


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("finance",)},
        {"data_domains": ("commerce", "finance")},
    ],
)
def test_file_ids_are_creator_tenant_project_and_exact_domain_private(tmp_path, change):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "private")
    other = actor().model_copy(update=change)
    other_token, _ = credentials(service.broker, other)
    for operation in [service.preview, service.result]:
        with pytest.raises(PermissionError):
            operation(preview.file.file_id, other, other_token)
    with pytest.raises(PermissionError):
        service.confirm(preview.file.file_id, other, other_token, confirmation(preview))


def test_confirmation_is_explicit_and_bound_to_preview_source_and_parser(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "confirm")
    client = client_for(service)
    path = f"/v1/analysis/files/csv/{preview.file.file_id}/confirm"
    fields = confirmation(preview).model_dump(mode="json")
    for update in (
        {"confirmed": False},
        {"confirmed": 1},
        {"path": "/etc/passwd"},
        {"sql": "DROP TABLE"},
    ):
        assert client.post(path, headers=headers, json=fields | update).status_code == 422
    assert (
        client.post(
            path, headers=headers, json=fields | {"source_hash": "sha256:" + "0" * 64}
        ).status_code
        == 409
    )
    from generaldataagent.datasets import csv_parser

    monkeypatch.setattr(csv_parser, "parser_fingerprint", lambda _: "sha256:" + "1" * 64)
    with pytest.raises(CsvIntakeConflict):
        service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    assert not list(service.artifact_root.rglob("*.json"))


def test_expiry_and_revocation_require_current_grant_without_extending_file_life(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=2)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "ttl")
    service.broker.revoke(token)
    with pytest.raises(PermissionError):
        service.preview(preview.file.file_id, actor(), token)
    fresh, _ = credentials(service.broker)
    assert service.preview(preview.file.file_id, actor(), fresh) == preview
    now[0] += timedelta(seconds=3)
    with pytest.raises(PermissionError):
        service.stage(RAW, actor(), fresh, "ttl")
    with pytest.raises(PermissionError):
        service.result(preview.file.file_id, actor(), fresh)


def test_retained_capacity_is_global_and_per_actor_including_expired_records(tmp_path):
    service = build_service(tmp_path, max_files=1, max_total=1)
    token, _ = credentials(service.broker)
    service.stage(RAW, actor(), token, "one")
    with pytest.raises(CsvIntakeConflict, match="capacity"):
        service.stage(RAW, actor(), token, "two")
    other = actor().model_copy(update={"user_id": "other"})
    other_token, _ = credentials(service.broker, other)
    with pytest.raises(CsvIntakeConflict, match="capacity"):
        service.stage(RAW, other, other_token, "other")


def test_default_off_shared_identity_and_production_constructor_gate(tmp_path):
    assert TestClient(create_app()).post("/v1/analysis/files/csv", content=RAW).status_code == 503
    service = build_service(tmp_path)
    with pytest.raises(ValueError, match="must_share_identity"):
        create_app(csv_intake=service)
    with pytest.raises(ValueError, match="not_qualified_for_production"):
        create_app(csv_intake=service, session_broker=service.broker, production_mode=True)
    _, headers = credentials(service.broker)
    client = client_for(service, service_enabled=lambda: False)
    assert (
        client.post(
            "/v1/analysis/files/csv", headers=headers | {"content-type": "text/csv"}, content=RAW
        ).status_code
        == 503
    )


@pytest.mark.parametrize("fault", ["audit", "artifact", "grant", "commit"])
def test_profile_failure_rolls_back_registration_and_cleans_scratch_and_artifacts(
    tmp_path, monkeypatch, fault
):
    import sqlite3

    from generaldataagent.artifacts.governed import GovernedArtifactStore

    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "failure")
    emit = service.audit.emit
    connect = sqlite3.connect
    profile = DataEngineeringRuntime.profile_relation
    with monkeypatch.context() as patch:
        if fault == "audit":
            patch.setattr(
                service.audit, "emit", lambda _: (_ for _ in ()).throw(OSError("audit failure"))
            )
        elif fault == "artifact":
            write = GovernedArtifactStore.put_bytes

            def failing_write(self, **kwargs):
                if kwargs["artifact_type"] == "csv_profile_provenance":
                    raise OSError("provenance storage failure")
                return write(self, **kwargs)

            patch.setattr(GovernedArtifactStore, "put_bytes", failing_write)
        elif fault == "grant":

            def revoked(*args, **kwargs):
                result = profile(*args, **kwargs)
                service.broker.revoke(token)
                return result

            patch.setattr(DataEngineeringRuntime, "profile_relation", revoked)
        else:

            class FailedCommit(sqlite3.Connection):
                def __exit__(self, exc_type, exc, traceback):
                    if exc_type is None:
                        self.rollback()
                        raise sqlite3.OperationalError("synthetic CSV registration commit failure")
                    return super().__exit__(exc_type, exc, traceback)

            def failing_connect(database, *args, **kwargs):
                if database == service.store.path:
                    kwargs["factory"] = FailedCommit
                return connect(database, *args, **kwargs)

            patch.setattr(sqlite3, "connect", failing_connect)
        with pytest.raises((CsvIntakeUnavailable, PermissionError, OSError)):
            service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    assert not list(service.scratch_root.iterdir())
    assert not list(service.artifact_root.rglob("*.json"))
    with service.store.db() as db:
        row = db.execute(
            "SELECT report FROM csv_uploads WHERE file_id=?", (str(preview.file.file_id),)
        ).fetchone()
        assert row["report"] is None
    # Fresh grant, same accepted bytes/preview; spent budget was not reset by rollback.
    fresh, _ = credentials(service.broker)
    service.audit.emit = emit
    result = service.confirm(preview.file.file_id, actor(), fresh, confirmation(preview))
    assert result.file.file_id == preview.file.file_id and result.profile.row_count == 3


@pytest.mark.parametrize("fault", ["source", "report", "artifact", "wrong_key", "schema"])
def test_source_report_or_evidence_corruption_never_falls_back_to_new_execution(
    tmp_path, monkeypatch, fault
):
    import sqlite3

    from generaldataagent.datasets.csv_intake_store import CsvIntakeUnavailable

    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "tamper")
    result = service.confirm(preview.file.file_id, actor(), token, confirmation(preview))

    def no_profile(*args, **kwargs):
        raise AssertionError("never re-execute to repair tampered evidence")

    monkeypatch.setattr(DataEngineeringRuntime, "profile_relation", no_profile)
    if fault == "wrong_key":
        with pytest.raises(PermissionError):
            CsvUploadStore(service.store.path, encryption_key=b"x" * 32)
        return
    if fault == "artifact":
        ref = result.evidence[0]
        (service.artifact_root / str(ref.task_id) / f"{ref.artifact_id}.json").write_text("{}")
    else:
        with sqlite3.connect(service.store.path) as db:
            if fault == "schema":
                db.execute("PRAGMA user_version=2")
            elif fault == "source":
                db.execute("UPDATE csv_uploads SET body=X'00'")
            else:
                db.execute("UPDATE csv_uploads SET report=X'00'")
    with pytest.raises((PermissionError, CsvIntakeConflict, CsvIntakeUnavailable)):
        service.result(preview.file.file_id, actor(), token)


def test_governed_evidence_remains_creator_private_even_with_guessed_ref(tmp_path):
    from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore

    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "artifact-owner")
    result = service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    other = actor().model_copy(update={"user_id": "other"})
    artifacts = GovernedArtifactStore(
        service.artifact_root,
        index_path=service.artifact_index,
        actor=other,
        read_policy=ArtifactReadPolicy.CREATOR,
    )
    with pytest.raises(PermissionError):
        artifacts.export_bytes(result.evidence[0])


def test_partial_stream_revocation_oversize_compression_and_timeout_leave_no_staging(tmp_path):
    import asyncio

    import httpx

    service = build_service(tmp_path, limits=CsvLimits(max_seconds=1, max_bytes=64))
    token, headers = credentials(service.broker)
    app = create_app(session_broker=service.broker, csv_intake=service)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
        ) as client:
            url = "/v1/analysis/files/csv"
            csv_headers = headers | {"content-type": "text/csv"}
            assert (
                await client.post(url, headers=csv_headers, content=b"a\n" + b"x" * 65)
            ).status_code == 413
            assert (
                await client.post(
                    url, headers=csv_headers | {"content-encoding": "gzip"}, content=RAW
                )
            ).status_code == 415
            assert (
                await client.post(
                    url,
                    headers=headers | {"content-type": "application/json"},
                    json={"path": "/etc/passwd"},
                )
            ).status_code == 415

            async def truncated():
                yield b"a\n"
                raise httpx.StreamError("synthetic truncated upload")

            assert (
                await client.post(url, headers=csv_headers, content=truncated())
            ).status_code >= 400

            async def delayed():
                yield b"a\n"
                await asyncio.sleep(1.1)
                yield b"x\n"

            assert (await client.post(url, headers=csv_headers, content=delayed())).status_code in {
                408,
                413,
            }

            async def revoked():
                yield b"a\n"
                service.broker.revoke(token)
                yield b"x\n"

            assert (await client.post(url, headers=csv_headers, content=revoked())).status_code in {
                401,
                404,
            }

    asyncio.run(exercise())
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 0


def test_csv_connector_connections_are_actually_closed_after_read_and_error(tmp_path, monkeypatch):
    import sqlite3

    from generaldataagent.connectors.sqlite import SQLiteConnector

    path = tmp_path / "fixture.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fixture (value TEXT)")
    connector = SQLiteConnector(path)
    connect = sqlite3.connect
    closed = []

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=ObservedConnection)
    )
    assert connector.fetch_all("SELECT 1") == ((1,),)
    with pytest.raises(ConnectorError):
        connector.fetch_all("SELECT * FROM missing")
    assert len(closed) == 2


def test_concurrent_confirmation_commits_one_profile_and_preserves_original_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "concurrent")
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: service.confirm(
                    preview.file.file_id, actor(), token, confirmation(preview)
                ),
                range(2),
            )
        )
    assert results[0] == results[1]
    assert len(list(service.artifact_root.rglob("*.json"))) == 2
    assert not list(service.scratch_root.iterdir())


def test_failed_execution_budget_is_not_reset_by_retry_but_completed_report_remains_readable(
    tmp_path, monkeypatch
):
    from generaldataagent.policy.budgets import BudgetExceededError

    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0])
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "budget-fail")
    execute = DataEngineeringRuntime.profile_relation

    def failed(*args, **kwargs):
        execute(*args, **kwargs)
        raise OSError("synthetic post-profile failure")

    with monkeypatch.context() as patch:
        patch.setattr(DataEngineeringRuntime, "profile_relation", failed)
        with pytest.raises(OSError):
            service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    now[0] += timedelta(seconds=11)
    with pytest.raises(BudgetExceededError):
        service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    assert not list(service.artifact_root.rglob("*.json"))
    fresh = service.stage(RAW, actor(), token, "explicit-new-task")
    result = service.confirm(fresh.file.file_id, actor(), token, confirmation(fresh))
    now[0] += timedelta(seconds=11)
    assert service.result(fresh.file.file_id, actor(), token) == result


def test_stream_actual_byte_count_overrules_forged_content_length(tmp_path):
    service = build_service(tmp_path, limits=CsvLimits(max_bytes=8))
    _, headers = credentials(service.broker)
    client = client_for(service)
    response = client.post(
        "/v1/analysis/files/csv",
        headers=headers | {"content-type": "text/csv", "content-length": "1"},
        content=b"a\nlonger_than_limit\n",
    )
    assert response.status_code == 413
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 0


def test_loaded_parser_and_processing_epoch_are_bound_without_hot_file_reinterpretation(
    tmp_path, monkeypatch
):
    from generaldataagent.datasets import csv_intake, csv_parser

    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "epoch")
    before = csv_parser.parser_fingerprint(service.limits)
    with monkeypatch.context() as patch:
        patch.setattr(csv_parser.Path, "read_bytes", lambda _: b"changed on disk after import")
        assert csv_parser.parser_fingerprint(service.limits) == before
    monkeypatch.setattr(csv_intake, "_PROFILE_FINGERPRINT", "sha256:" + "0" * 64)
    with pytest.raises(CsvIntakeConflict):
        service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    assert not list(service.artifact_root.rglob("*.json"))


def test_large_cell_is_bounded_before_process_global_csv_reader_allocation(monkeypatch):
    from generaldataagent.datasets import csv_parser

    def no_reader(*args, **kwargs):
        raise AssertionError("oversized raw field reached the stdlib reader")

    monkeypatch.setattr(csv_parser.csv, "reader", no_reader)
    with pytest.raises(CsvIntakeLimit):
        csv_parser.parse_csv(b"a\n" + b"x" * 10000, CsvLimits(), lambda: None)


def test_api_identity_permission_and_confirmation_body_limits(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    client = client_for(service)
    url = "/v1/analysis/files/csv"
    assert (
        client.post(
            url, headers={"content-type": "text/csv", "idempotency-key": "x"}, content=RAW
        ).status_code
        == 401
    )
    _, no_permission = credentials(service.broker, permissions=frozenset())
    assert (
        client.post(
            url, headers=no_permission | {"content-type": "text/csv"}, content=RAW
        ).status_code
        == 403
    )
    assert (
        client.post(
            url, headers=headers | {"x-tenant-id": "other", "content-type": "text/csv"}, content=RAW
        ).status_code
        == 403
    )
    preview = service.stage(RAW, actor(), token, "body")
    path = url + f"/{preview.file.file_id}/confirm"
    assert (
        client.post(
            path, headers=headers | {"content-type": "application/json"}, content=b"x" * 1025
        ).status_code
        == 413
    )
    assert service.preview(preview.file.file_id, actor(), token).file.status == "staged"


def test_actual_csv_asgi_demo_reopens_private_state_and_profiles_confirmed_source():
    import json
    import subprocess
    import sys
    from pathlib import Path

    process = subprocess.run(
        [sys.executable, "-m", "examples.csv_intake_demo"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["asgi_api"] and report["state_reopened"] and report["scratch_clean"]
    assert report["preview_rows"] == 3 and report["empty_cells"] == [0, 0, 1]
    assert report["duplicate_rows"] == 1 and report["formula_like_cells"] == 2
    assert report["profile_status"] == "profiled" and report["text_only"]
    assert report["evidence_types"] == ["csv_profile_provenance", "data_profile"]
    assert report["repeated_confirmation_same_report"] and report["model_calls"] == 0
    assert not report["certified_metric_registered"]
