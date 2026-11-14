import base64
import csv
import hashlib
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from errno import ENOSPC
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import prepared
from test_stage13_xlsx_intake import build_service, client_for, declaration_body, declare
from test_stage13_xlsx_snapshot import snapshot

from generaldataagent.contracts import ArtifactClassification, AuditEvent, ToolRequest
from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_export import (
    XlsxColumnExport,
    XlsxExportConflict,
    XlsxExportReceipt,
    XlsxExportRefused,
    XlsxExportRequest,
    XlsxExportUnavailable,
    _csv_content,
    _publish_private,
    _snapshot_content_hash,
    main,
)
from generaldataagent.datasets.xlsx_intake_contracts import XlsxSnapshotColumnData, XlsxSnapshotData
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy import PolicyEngine
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.dlp import DLPDeniedError
from generaldataagent.security.identity import SessionBroker

KEY = b"i" * 32
NOW = datetime.now(UTC).replace(microsecond=0)
NOOP = lambda: None  # noqa: E731 - deterministic helper for pure CSV projection tests


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def key_file(tmp_path, key=KEY):
    path = tmp_path / "xlsx-key"
    path.write_bytes(base64.b64encode(key))
    path.chmod(0o600)
    return path


def token_file(tmp_path, token):
    path = tmp_path / "token"
    path.write_text(token, encoding="ascii")
    path.chmod(0o600)
    return path


def approve(
    tmp_path,
    clock,
    request,
    *,
    actor_context=None,
    mutate=None,
    approved=True,
    expiry=None,
):
    owner = actor_context or actor()
    store = SQLiteApprovalStore(tmp_path / "approvals.db", clock=clock)
    tool_request = request.tool_request(owner, uuid4())
    if mutate is not None:
        tool_request = mutate(tool_request)
    created = store.create_request(
        tool_request,
        impact_scope="One bounded CSV page of selected declared columns",
        cost_estimate="One local read-only export",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="Approval is one-time; output file is operator-owned",
        expires_at=expiry or datetime.now(UTC) + timedelta(hours=1),
    )
    if approved:
        store.approve(created.approval_id, approver_id="independent-reviewer")
    return created.approval_id


def export_for(
    tmp_path,
    clock,
    request,
    approval_id,
    *,
    owner=None,
    token=None,
    broker=None,
    audit=None,
    key=KEY,
    scanner=None,
    limits=None,
):
    return XlsxColumnExport(
        store_path=tmp_path / "xlsx.db",
        encryption_key=key,
        approval_db=tmp_path / "approvals.db",
        approval_id=approval_id,
        actor=owner or actor(),
        session_token=token or "",
        broker=broker or SessionBroker(clock=clock),
        audit=audit or JsonlAuditSink(tmp_path / "export-audit.jsonl"),
        request=request,
        clock=clock,
        limits=limits,
        scanner=scanner,
    )


def prepared_export(
    tmp_path, clock, *, columns=(0, 2), after=0, limit=500, build=None, retention=86400
):
    service = build_service(tmp_path, clock=clock, retention=retention)
    owner = actor()
    token, headers = credentials(service.broker, owner, frozenset({"analysis:run"}))
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    snapshot_hash = client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot", headers=headers
    ).json()["snapshot_hash"]
    request = XlsxExportRequest(
        file_id=file_id,
        snapshot_hash=snapshot_hash,
        columns=columns,
        after=after,
        limit=limit,
        instance_id=uuid4(),
    )
    approved = build or approve
    approval_id = approved(tmp_path, clock, request, actor_context=owner)
    return service, owner, token, request, approval_id


def export_events(tmp_path):
    path = tmp_path / "export-audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def grant_unused(tmp_path, clock, approval_id):
    return (
        not SQLiteApprovalStore(tmp_path / "approvals.db", clock=clock)
        .inspect(approval_id)
        .consumed
    )


def test_export_returns_only_approved_columns_and_formula_safe_values(tmp_path):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    before = (tmp_path / "xlsx.db").read_bytes()
    export = export_for(
        tmp_path,
        clock,
        request,
        approval_id,
        owner=owner,
        token=token,
        broker=service.broker,
    )
    content, receipt = export.execute()
    assert (tmp_path / "xlsx.db").read_bytes() == before
    rows = list(csv.reader(io.StringIO(content.decode(), newline="")))
    assert rows[0] == ["name", "flag"]
    assert [row[0] for row in rows[1:]] == ["alpha", "beta", "alpha", "gamma", "delta", "one", ""]
    assert [row[1] for row in rows[1:]] == ["true", "false", "true", "", "true", "false", "true"]
    assert "amount" not in content.decode() and "2.5" not in content.decode()
    assert receipt.rows == 7 and receipt.cells == 14
    assert receipt.content_sha256 == "sha256:" + hashlib.sha256(content).hexdigest()
    assert receipt.artifact is False and receipt.dataset_snapshot is False
    assert receipt.audit == "xlsx.columns_exported"
    assert receipt.approval == "one-time-grant-consumed"
    events = export_events(tmp_path)
    assert len(events) == 1 and events[0]["event_type"] == "xlsx.columns_exported"
    assert events[0]["actor_id"] == owner.user_id
    assert events[0]["payload"]["rows"] == 7
    assert events[0]["payload"]["content_sha256"] == receipt.content_sha256
    assert "alpha" not in json.dumps(events) and "flag" not in json.dumps(events)
    with pytest.raises(XlsxExportRefused):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert len(export_events(tmp_path)) == 1


def test_export_bounded_keyset_and_formula_prefixes_are_quoted():
    columns = (
        XlsxSnapshotColumnData(
            index=0,
            header="=formula",
            type="text",
            statuses="vvvv",
            values=("=1+1", "+2", "-3", "@x"),
        ),
        XlsxSnapshotColumnData(
            index=1, header="code", type="text", statuses="vtum", values=("abc",)
        ),
    )
    data = XlsxSnapshotData(
        snapshot_hash="sha256:" + "0" * 64,
        source_hash="sha256:" + "1" * 64,
        preview_hash="sha256:" + "2" * 64,
        confirmation_hash="sha256:" + "3" * 64,
        interpretation_hash="sha256:" + "4" * 64,
        created_at=NOW,
        header_row=1,
        date_system="1900",
        rows=(2, 3, 4, 5),
        columns=columns,
        duplicate_rows="reject",
        duplicate_row_count=0,
    )
    digest = hashlib.sha256(b"gda-xlsx-derived-snapshot-v1\n")
    digest.update(data.interpretation_hash.encode() + b"\n")
    digest.update(b'[["value","=1+1"],["value","abc"]]\n')
    digest.update(b'[["value","+2"],["not_trusted",null]]\n')
    digest.update(b'[["value","-3"],["unparsed",null]]\n')
    digest.update(b'[["value","@x"],["missing",null]]\n')
    assert _snapshot_content_hash(data, NOOP) == "sha256:" + digest.hexdigest()
    data = data.model_copy(update={"snapshot_hash": "sha256:" + digest.hexdigest()})
    first = XlsxExportRequest(
        file_id=uuid4(),
        snapshot_hash=data.snapshot_hash,
        columns=(0, 1),
        after=0,
        limit=2,
        instance_id=uuid4(),
    )
    content, rows = _csv_content(data, first, NOOP)
    assert rows == 2
    assert list(csv.reader(io.StringIO(content.decode(), newline=""))) == [
        ["'=formula", "code"],
        ["'=1+1", "abc"],
        ["'+2", ""],
    ]
    second = first.model_copy(update={"after": 3, "limit": 2})
    content, rows = _csv_content(data, second, NOOP)
    assert rows == 2
    assert list(csv.reader(io.StringIO(content.decode(), newline=""))) == [
        ["'=formula", "code"],
        ["'-3", ""],
        ["'@x", ""],
    ]
    with pytest.raises(XlsxExportConflict):
        _csv_content(data, first.model_copy(update={"columns": (0, 3)}), NOOP)
    corrupted = data.model_copy(
        update={
            "columns": (
                data.columns[0].model_copy(update={"values": ("changed", "+2", "-3", "@x")}),
                data.columns[1],
            )
        }
    )
    with pytest.raises(XlsxExportConflict, match="xlsx_export_snapshot_changed"):
        _csv_content(corrupted, first, NOOP)


def test_export_refuses_mismatched_approval_before_consuming(tmp_path):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    wrong = request.model_copy(update={"columns": (1,)})
    with pytest.raises(XlsxExportRefused, match="xlsx_export_approval_unavailable"):
        export_for(
            tmp_path,
            clock,
            wrong,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    same = request.model_copy(update={"instance_id": uuid4()})
    with pytest.raises(XlsxExportRefused, match="xlsx_export_approval_unavailable"):
        export_for(
            tmp_path,
            clock,
            same,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)
    assert not export_events(tmp_path)
    assert (
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        )
        .execute()[1]
        .rows
        == 7
    )


def test_export_binds_full_tool_request_and_policy(tmp_path):
    clock = Clock()
    owner = actor()
    request = XlsxExportRequest(
        file_id=uuid4(),
        snapshot_hash="sha256:" + "0" * 64,
        columns=(0,),
        instance_id=uuid4(),
    )
    tool_request = request.tool_request(owner, uuid4()).model_copy(
        update={"agent_role": "reporter"}
    )
    approval_id = approve(tmp_path, clock, request, mutate=lambda _: tool_request)
    assert grant_unused(tmp_path, clock, approval_id)
    broker = SessionBroker(clock=clock)
    token = broker.issue(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        data_domains=frozenset(owner.data_domains),
        permissions=frozenset({"analysis:run"}),
        ttl_seconds=900,
    )
    with pytest.raises(XlsxExportRefused, match="xlsx_export_approval_unavailable"):
        export_for(
            tmp_path, clock, request, approval_id, owner=owner, token=token, broker=broker
        ).execute()
    decision = PolicyEngine().evaluate(request.tool_request(owner, uuid4()))
    assert decision.decision == "require_approval" and decision.risk_level == "r2"
    assert set(decision.obligations) >= {"audit", "approval", "dlp"}
    assert (
        PolicyEngine()
        .evaluate(request.tool_request(owner, uuid4()).model_copy(update={"data_domains": ()}))
        .reason_code
        == "missing_data_domain"
    )


def test_export_refuses_unapproved_expired_and_revoked_grants(tmp_path):
    clock = Clock()
    service, owner, token, request, _ = prepared_export(tmp_path, clock)
    unapproved = approve(tmp_path, clock, request, approved=False)
    with pytest.raises(XlsxExportRefused, match="xlsx_export_approval_unavailable"):
        export_for(
            tmp_path,
            clock,
            request,
            unapproved,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    expired = approve(tmp_path, clock, request, expiry=datetime.now(UTC) + timedelta(minutes=5))
    clock.now += timedelta(minutes=10)
    with pytest.raises(XlsxExportRefused, match="xlsx_export_approval_unavailable"):
        export_for(
            tmp_path,
            clock,
            request,
            expired,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert grant_unused(tmp_path, clock, expired)
    clock.now = NOW
    approval_id = approve(tmp_path, clock, request)
    service.broker.revoke(token)
    with pytest.raises(PermissionError, match="invalid_or_expired_session"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)


def test_export_denies_wrong_creator_and_business_constraint(tmp_path):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    other = owner.model_copy(update={"user_id": "other"})
    with pytest.raises(XlsxExportRefused, match="xlsx_export_authorization_unavailable"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=other,
            token=token,
            broker=service.broker,
        ).execute()

    class RevokingBroker(SessionBroker):
        def __init__(self):
            super().__init__(clock=clock)
            self.checked = False

        def authenticate_reference(self, reference):
            identity = super().authenticate_reference(reference)
            if not self.checked:
                return identity
            self.revoke(token)
            raise PermissionError("session_revoked_mid_export")

    broker = RevokingBroker()
    second_token = broker.issue(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        data_domains=frozenset(owner.data_domains),
        permissions=frozenset({"analysis:run"}),
        ttl_seconds=900,
    )
    broker.authenticate_reference(broker.session_reference(second_token))
    export = export_for(
        tmp_path,
        clock,
        request,
        approval_id,
        owner=owner,
        token=second_token,
        broker=broker,
        audit=JsonlAuditSink(tmp_path / "blocked-audit.jsonl"),
    )
    broker.checked = True
    with pytest.raises(PermissionError, match="session_revoked_mid_export"):
        export.execute()
    assert grant_unused(tmp_path, clock, approval_id)
    assert not (tmp_path / "blocked-audit.jsonl").exists()


def test_export_refuses_drift_without_consuming(tmp_path, monkeypatch):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    with pytest.raises(XlsxExportConflict, match="xlsx_export_snapshot_changed"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)
    assert not export_events(tmp_path)


def test_export_dlp_denial_does_not_consume_approval(tmp_path):
    class BlockingScanner:
        def __init__(self):
            self.calls = 0

        def check_export(self, content: bytes, classification) -> None:
            self.calls += 1
            assert classification == ArtifactClassification.INTERNAL
            raise DLPDeniedError("dlp_personal_data")

    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    scanner = BlockingScanner()
    with pytest.raises(DLPDeniedError):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
            scanner=scanner,
        ).execute()
    assert scanner.calls == 1
    assert grant_unused(tmp_path, clock, approval_id)
    assert not export_events(tmp_path)


def test_export_audit_failure_does_not_consume_approval(tmp_path):
    class BrokenAudit:
        def emit(self, event: AuditEvent) -> None:
            raise OSError("audit-unavailable")

    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    with pytest.raises(XlsxExportUnavailable, match="xlsx_export_audit_unavailable"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
            audit=BrokenAudit(),
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)


def test_export_expiring_during_audit_does_not_consume_approval(tmp_path):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock, retention=60)

    class Audit:
        def emit(self, event: AuditEvent) -> None:
            clock.now += timedelta(seconds=60)

    with pytest.raises(XlsxExportRefused, match="xlsx_export_file_unavailable"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
            audit=Audit(),
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)


def test_export_key_layout_and_missing_source_fail_closed(tmp_path):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(tmp_path, clock)
    with pytest.raises(XlsxExportRefused, match="xlsx_export_key_mismatch"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
            key=b"j" * 32,
        ).execute()
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(XlsxExportUnavailable, match="xlsx_export_layout_unsupported"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        db.execute("PRAGMA user_version=1")
        db.execute("DELETE FROM xlsx_uploads")
    with pytest.raises(XlsxExportConflict, match="xlsx_export_source_missing"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)
    assert not export_events(tmp_path)


def test_export_refuses_undeclared_column_and_declaration_drift(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock)
    owner = actor()
    token, headers = credentials(service.broker, owner, frozenset({"analysis:run"}))
    client = client_for(service)
    file_id, declared = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    snapshot_hash = client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot", headers=headers
    ).json()["snapshot_hash"]

    def one_column(column):
        request = XlsxExportRequest(
            file_id=file_id,
            snapshot_hash=snapshot_hash,
            columns=(column,),
            instance_id=uuid4(),
        )
        approval_id = approve(tmp_path, clock, request, actor_context=owner)
        return (
            export_for(
                tmp_path,
                clock,
                request,
                approval_id,
                owner=owner,
                token=token,
                broker=service.broker,
            )
            .execute()[1]
            .columns
        )

    assert one_column(0) == (0,)
    narrow = declaration_body(declared, columns=[{"index": 0, "header": "name", "type": "text"}])
    response = declare(client, headers, file_id, narrow)
    assert response.status_code == 200, response.text
    assert snapshot(client, headers, file_id).status_code == 200
    stale_hash = client.get(f"/v1/analysis/files/xlsx/{file_id}/snapshot", headers=headers).json()[
        "snapshot_hash"
    ]
    request = XlsxExportRequest(
        file_id=file_id,
        snapshot_hash=stale_hash,
        columns=(2,),
        instance_id=uuid4(),
    )
    approval_id = approve(tmp_path, clock, request, actor_context=owner)
    with pytest.raises(XlsxExportConflict, match="xlsx_export_column_not_declared"):
        export_for(
            tmp_path,
            clock,
            request,
            approval_id,
            owner=owner,
            token=token,
            broker=service.broker,
        ).execute()
    assert grant_unused(tmp_path, clock, approval_id)


def test_cli_writes_one_private_output_and_reports_exit_codes(tmp_path, capsys, monkeypatch):
    clock = Clock()
    service, owner, token, request, approval_id = prepared_export(
        tmp_path,
        clock,
        columns=(0,),
        build=lambda _, clock, request, **kwargs: approve(
            tmp_path, clock, request, actor_context=kwargs["actor_context"]
        ),
    )
    out = tmp_path / "export.csv"
    argv = [
        "--store",
        str(tmp_path / "xlsx.db"),
        "--key-file",
        str(key_file(tmp_path)),
        "--approval-db",
        str(tmp_path / "approvals.db"),
        "--approval-id",
        str(approval_id),
        "--sessions-db",
        str(service.broker.path),
        "--token-file",
        str(token_file(tmp_path, token)),
        "--audit-log",
        str(tmp_path / "export-audit.jsonl"),
        "--user-id",
        owner.user_id,
        "--tenant-id",
        owner.tenant_id,
        "--project-id",
        owner.project_id,
        "--data-domain",
        owner.data_domains[0],
        "--file-id",
        str(request.file_id),
        "--snapshot-hash",
        request.snapshot_hash,
        "--columns",
        "0",
        "--instance-id",
        str(request.instance_id),
        "--out",
        str(out),
    ]
    out.write_bytes(b"existing output")
    assert main(argv) == 5
    assert out.read_bytes() == b"existing output"
    assert grant_unused(tmp_path, clock, approval_id)
    assert export_events(tmp_path) == []
    capsys.readouterr()
    out.unlink()

    def disk_full(_descriptor):
        raise OSError(ENOSPC, "synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr("generaldataagent.datasets.xlsx_export.os.fsync", disk_full)
        assert main(argv) == 4
    assert grant_unused(tmp_path, clock, approval_id)
    assert not out.exists()
    assert list(tmp_path.glob(".gda-xlsx-*")) == []
    capsys.readouterr()

    def concurrent_output(_descriptor):
        out.write_bytes(b"concurrent output")

    with monkeypatch.context() as patch:
        patch.setattr("generaldataagent.datasets.xlsx_export.os.fsync", concurrent_output)
        assert main(argv) == 5
    assert grant_unused(tmp_path, clock, approval_id)
    assert out.read_bytes() == b"concurrent output"
    assert list(tmp_path.glob(".gda-xlsx-*")) == []
    capsys.readouterr()
    out.unlink()

    assert main(argv) == 0
    receipt = XlsxExportReceipt.model_validate_json(capsys.readouterr().out.strip())
    assert receipt.columns == (0,) and receipt.rows == 7
    assert out.read_bytes().startswith(b"name\r\n")
    assert out.stat().st_mode & 0o077 == 0
    assert main(argv[:-1] + [str(tmp_path / "second.csv")]) == 2
    capsys.readouterr()
    with pytest.raises(XlsxExportConflict, match="xlsx_export_output_exists"):
        _publish_private(out, out)
    assert out.read_bytes().startswith(b"name\r\n")
    wrong = list(argv)
    wrong[wrong.index("--columns") + 1] = "1"
    assert main(wrong[:-1] + [str(tmp_path / "third.csv")]) == 2
    with pytest.raises(ValidationError, match="xlsx_export_receipt_invalid"):
        XlsxExportReceipt.model_validate(receipt.model_dump() | {"cells": 99})
    assert len(export_events(tmp_path)) == 1
    assert token not in (tmp_path / "export-audit.jsonl").read_text()
    capsys.readouterr()

    late_approval = approve(tmp_path, clock, request, actor_context=owner)
    late_argv = [*argv[:-1], str(tmp_path / "late.csv")]
    late_argv[late_argv.index("--approval-id") + 1] = str(late_approval)

    def failed_publish(_staged, _destination):
        raise FileExistsError("synthetic destination race")

    with monkeypatch.context() as patch:
        patch.setattr("generaldataagent.datasets.xlsx_export.os.link", failed_publish)
        assert main(late_argv) == 4
    assert capsys.readouterr().err.strip() == "xlsx_export_publish_failed_after_consumption"
    assert not grant_unused(tmp_path, clock, late_approval)
    assert not (tmp_path / "late.csv").exists()
    assert list(tmp_path.glob(".gda-xlsx-*")) == []


def test_export_request_contract_invariants():
    file_id, instance_id = uuid4(), uuid4()
    with pytest.raises(ValidationError, match="xlsx_export_columns_invalid"):
        XlsxExportRequest(
            file_id=file_id,
            snapshot_hash="sha256:" + "0" * 64,
            columns=(1, 0),
            instance_id=instance_id,
        )
    with pytest.raises(ValidationError):
        XlsxExportRequest(
            file_id=file_id,
            snapshot_hash="sha256:" + "0" * 64,
            columns=(0,),
            limit=501,
            instance_id=instance_id,
        )
    first = XlsxExportRequest(
        file_id=file_id,
        snapshot_hash="sha256:" + "0" * 64,
        columns=(0,),
        instance_id=instance_id,
    )
    second = first.model_copy(update={"instance_id": uuid4()})
    assert first.export_hash != second.export_hash
    assert set(first.arguments) == {"export_hash", "file_ids", "columns", "after", "limit"}
    owner = actor()
    tool_request = first.tool_request(owner, uuid4())
    assert isinstance(tool_request, ToolRequest)
    assert tool_request.resource == first.export_hash
    assert tool_request.user_id == owner.user_id
    assert tool_request.requested_risk == "r2"
    assert UUID(str(tool_request.task_id))
