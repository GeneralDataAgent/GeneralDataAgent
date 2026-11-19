import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import prepared
from test_stage13_xlsx_intake import (
    XLSX,
    build_service,
    client_for,
    confirm,
    confirm_body,
    declaration_body,
    declare,
)
from test_stage13_xlsx_interpretation import bool_cell
from test_stage13_xlsx_preflight import package
from test_stage13_xlsx_preview import entries_for, number_cell, row, sheet, text_cell
from test_stage13_xlsx_snapshot import snapshot

from generaldataagent.api.app import create_app
from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.contracts import ArtifactRef
from generaldataagent.datasets.xlsx_analysis import (
    BindXlsxAnalysis,
    ReadXlsxAnalysisMetric,
    XlsxAnalysisArtifact,
    XlsxAnalysisBinder,
    XlsxAnalysisColumn,
    XlsxAnalysisColumnProfile,
    XlsxAnalysisMetric,
    XlsxAnalysisProfile,
    XlsxAnalysisRefused,
    XlsxAnalysisValuePage,
    _exact_value,
    analysis_page,
    load_xlsx_analysis_artifact,
)
from generaldataagent.datasets.xlsx_intake_contracts import XlsxSnapshotColumnData
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.dlp import DLPDeniedError

HEADERS = ("code", "count", "active", "price", "day")
COLUMNS = [
    {"index": 0, "header": "code", "type": "text"},
    {"index": 1, "header": "count", "type": "integer"},
    {"index": 2, "header": "active", "type": "boolean"},
    {"index": 3, "header": "price", "type": "decimal"},
    {"index": 4, "header": "day", "type": "date"},
]
RAW = package(
    entries_for(
        sheet(
            row(
                1,
                *(
                    text_cell(f"{letter}1", name)
                    for letter, name in zip("ABCDE", HEADERS, strict=True)
                ),
            ),
            row(
                2,
                text_cell("A2", "001"),
                number_cell("B2", "2"),
                bool_cell("C2", "1"),
                number_cell("D2", "2.50"),
                number_cell("E2", "45000"),
            ),
            row(
                3,
                text_cell("A3", "002"),
                number_cell("B3", "3"),
                bool_cell("C3", "0"),
                number_cell("D3", "-0.10"),
                number_cell("E3", "45001"),
            ),
            row(
                4,
                text_cell("A4", "003"),
                number_cell("B4", "-4"),
                bool_cell("C4", "1"),
                number_cell("D4", "10"),
                number_cell("E4", "45002"),
            ),
        )
    )
)


def setup(tmp_path, **kwargs):
    intake = build_service(tmp_path, **kwargs)
    binder = XlsxAnalysisBinder(
        intake=intake,
        budgets=TaskBudgetLedger(tmp_path / "analysis-budgets.db", clock=intake.store.clock),
        artifact_root=tmp_path / "artifacts",
        artifact_index=tmp_path / "artifact-index.db",
    )
    client = TestClient(
        create_app(session_broker=intake.broker, xlsx_intake=intake, xlsx_analysis=binder)
    )
    token, headers = credentials(intake.broker)
    return intake, binder, client, token, headers


def bind(client, headers, file_id, digest, columns):
    return client.post(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/analysis",
        headers=headers | {"content-type": "application/json"},
        json={"schema_version": "1.0", "snapshot_hash": digest, "columns": list(columns)},
    )


def read_values(client, headers, file_id, artifact_id, digest, **params):
    return client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/analysis/{artifact_id}/values",
        headers=headers,
        params={"snapshot_hash": digest, **params},
    )


def reader(intake, tmp_path, owner=None):
    return GovernedArtifactStore(
        tmp_path / "artifacts",
        index_path=tmp_path / "artifact-index.db",
        actor=owner or actor(),
        scanner=intake.scanner,
        retention_seconds=intake.retention_seconds,
        clock=intake.store.clock,
        read_policy=ArtifactReadPolicy.CREATOR,
    )


def test_binding_requires_a_current_declared_snapshot_and_returns_metadata(tmp_path):
    intake, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]

    response = bind(client, headers, file_id, digest, (0, 1, 2, 3, 4))
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["reader"] == "gda-xlsx-analysis-binding-v1"
    assert report["snapshot_hash"] == digest
    assert report["scope"] == "selected-declared-columns"
    assert report["values"] == "typed-exact-not-returned"
    assert report["storage"] == "dlp-scanned-creator-artifact"
    assert report["rows"] == "present-value-only"
    assert report["roles"] == "not-assigned"
    assert report["target"] == "not-designated"
    assert report["duplicates"] == "all-present-rows-kept"
    assert report["formula_cached_values"] == "excluded"
    assert report["missing_values"] == "excluded"
    assert report["dataset_snapshot"] is False
    assert report["certified_metric_source"] is False
    assert (report["row_count"], report["cells"]) == (3, 15)
    assert (report["date_system"], report["duplicate_rows"]) == ("1900", "reject")
    assert report["duplicate_row_count"] == 0
    assert [column["index"] for column in report["columns"]] == [0, 1, 2, 3, 4]
    assert [column["analysis_type"] for column in report["columns"]] == [
        "string",
        "integer",
        "boolean",
        "string",
        "string",
    ]
    assert [column["conversion"] for column in report["columns"]] == [
        "exact-text",
        "exact-integer",
        "exact-boolean",
        "exact-decimal-text",
        "iso-date-text",
    ]
    body = json.dumps(report)
    for value in ("2023-03-15", '"2.5"'):
        assert value not in body
    assert "xlsx.analysis_dataset_bound" in (tmp_path / "audit.jsonl").read_text()

    artifact = ArtifactRef.model_validate(report["artifact"])
    assert artifact.artifact_type == "xlsx_analysis_dataset"
    assert artifact.content_hash == report["artifact"]["content_hash"]
    content = reader(intake, tmp_path).export_bytes(artifact)
    parsed = load_xlsx_analysis_artifact(content, artifact=artifact)
    assert parsed.file_id == UUID(file_id)
    assert parsed.task_id == artifact.task_id
    assert parsed.row_numbers == (2, 3, 4)
    assert parsed.rows == (
        ("001", 2, True, "2.5", (date(1899, 12, 31) + timedelta(days=44999)).isoformat()),
        ("002", 3, False, "-0.1", (date(1899, 12, 31) + timedelta(days=45000)).isoformat()),
        ("003", -4, True, "10", (date(1899, 12, 31) + timedelta(days=45001)).isoformat()),
    )
    assert type(parsed.rows[0][1]) is int and type(parsed.rows[0][2]) is bool
    assert type(parsed.rows[0][3]) is str


def test_binding_projects_only_the_explicitly_selected_columns(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (1, 4))
    assert response.status_code == 201, response.text
    report = response.json()
    assert [column["index"] for column in report["columns"]] == [1, 4]
    assert [column["header"] for column in report["columns"]] == ["count", "day"]
    assert (report["row_count"], report["cells"]) == (3, 6)


def test_exact_value_refuses_noncanonical_integer_and_boolean_text():
    integer = XlsxSnapshotColumnData(
        index=0, header="n", type="integer", statuses="v", values=("not-a-number",)
    )
    boolean = XlsxSnapshotColumnData(
        index=1, header="b", type="boolean", statuses="v", values=("yes",)
    )
    with pytest.raises(XlsxAnalysisRefused) as invalid_integer:
        _exact_value(integer, "not-a-number")
    assert invalid_integer.value.code == "xlsx_analysis_value_invalid"
    with pytest.raises(XlsxAnalysisRefused) as invalid_boolean:
        _exact_value(boolean, "yes")
    assert invalid_boolean.value.code == "xlsx_analysis_value_invalid"


def test_artifact_loader_rejects_a_foreign_type_or_task(tmp_path):
    intake, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact = ArtifactRef.model_validate(
        bind(client, headers, file_id, digest, (0,)).json()["artifact"]
    )
    content = reader(intake, tmp_path).export_bytes(artifact)
    with pytest.raises(ValueError, match="xlsx_analysis_artifact_type_invalid"):
        load_xlsx_analysis_artifact(
            content, artifact=artifact.model_copy(update={"artifact_type": "other"})
        )
    with pytest.raises(ValueError, match="xlsx_analysis_artifact_binding_invalid"):
        load_xlsx_analysis_artifact(
            content, artifact=artifact.model_copy(update={"task_id": UUID(int=0)})
        )


def test_binding_appends_a_new_artifact_for_each_explicit_request(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    first = bind(client, headers, file_id, digest, (0, 1))
    second = bind(client, headers, file_id, digest, (0, 1))
    assert first.status_code == second.status_code == 201
    assert first.json()["artifact"]["artifact_id"] != second.json()["artifact"]["artifact_id"]
    assert first.json()["artifact"]["content_hash"] == second.json()["artifact"]["content_hash"]


def test_binding_refuses_any_missing_formula_or_error_cell(tmp_path):
    intake, binder, client, token, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (0,))
    assert response.status_code == 422
    assert response.json()["detail"] == "xlsx_analysis_non_value_cells"
    with pytest.raises(XlsxAnalysisRefused) as refused:
        binder.bind(
            file_id,
            actor(),
            token,
            BindXlsxAnalysis(snapshot_hash=digest, columns=(0,)),
        )
    assert refused.value.code == "xlsx_analysis_non_value_cells"
    assert refused.value.columns == {0: {"v": 6, "m": 1}}
    assert not (tmp_path / "artifacts").exists()


def test_binding_requires_a_derived_snapshot(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    response = bind(client, headers, file_id, "sha256:" + "0" * 64, (0,))
    assert response.status_code == 409
    assert response.json()["detail"] == "xlsx_snapshot_not_derived"


def test_binding_requires_declared_columns(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS[:3])
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (0, 4))
    assert response.status_code == 422
    assert response.json()["detail"] == "xlsx_analysis_column_not_declared"


def test_binding_requires_non_empty_column_names(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    empty = package(
        entries_for(
            sheet(
                row(1, text_cell("A1", ""), text_cell("B1", "")),
                row(2, text_cell("A2", "a"), text_cell("B2", "b")),
                row(3, text_cell("A3", "c"), text_cell("B3", "d")),
            )
        )
    )
    file_id, _ = prepared(
        client,
        headers,
        raw=empty,
        columns=[
            {"index": 0, "header": "", "type": "text"},
            {"index": 1, "header": "", "type": "text"},
        ],
    )
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (0,))
    assert response.status_code == 422
    assert response.json()["detail"] == "xlsx_analysis_column_names_invalid"


def test_binding_requires_unique_column_names(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    duplicate = package(
        entries_for(
            sheet(
                row(1, text_cell("A1", "same"), text_cell("B1", "same")),
                row(2, text_cell("A2", "a"), text_cell("B2", "b")),
                row(3, text_cell("A3", "c"), text_cell("B3", "d")),
            )
        )
    )
    file_id, _ = prepared(
        client,
        headers,
        raw=duplicate,
        columns=[
            {"index": 0, "header": "same", "type": "text"},
            {"index": 1, "header": "same", "type": "text"},
        ],
    )
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (0, 1))
    assert response.status_code == 422
    assert response.json()["detail"] == "xlsx_analysis_column_names_invalid"


def test_binding_requires_at_least_two_present_rows(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    single = package(
        entries_for(
            sheet(
                row(1, text_cell("A1", "code")),
                row(2, text_cell("A2", "only")),
            )
        )
    )
    file_id, _ = prepared(
        client, headers, raw=single, columns=[{"index": 0, "header": "code", "type": "text"}]
    )
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (0,))
    assert response.status_code == 422
    assert response.json()["detail"] == "xlsx_analysis_rows_insufficient"


def test_binding_is_bound_to_the_exact_snapshot_version(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers, raw=RAW, columns=COLUMNS)
    old = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = declare(client, headers, file_id, declaration_body(declared, columns=[COLUMNS[0]]))
    assert response.status_code == 200, response.text
    stale = bind(client, headers, file_id, old, (0,))
    assert stale.status_code == 409
    assert stale.json()["detail"] == "xlsx_snapshot_stale"
    new = snapshot(client, headers, file_id).json()["snapshot_hash"]
    changed = bind(client, headers, file_id, old, (0,))
    assert changed.status_code == 409
    assert changed.json()["detail"] == "xlsx_snapshot_changed"
    assert bind(client, headers, file_id, new, (0,)).status_code == 201


def test_binding_is_creator_scoped_and_expires(tmp_path):
    now = [datetime(2026, 11, 18, 1, 0, tzinfo=UTC)]
    intake, _, client, _, headers = setup(tmp_path, clock=lambda: now[0], retention=60)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    assert bind(client, headers, file_id, digest, (0,)).status_code == 201
    other = actor().model_copy(update={"user_id": "other", "tenant_id": "tenant-2"})
    _, other_headers = credentials(intake.broker, other)
    assert bind(client, other_headers, file_id, digest, (0,)).status_code == 404
    now[0] += timedelta(seconds=61)
    assert bind(client, headers, file_id, digest, (0,)).status_code == 404


def test_binding_artifact_is_creator_readable_only(tmp_path):
    intake, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact = ArtifactRef.model_validate(
        bind(client, headers, file_id, digest, (0,)).json()["artifact"]
    )
    other = actor().model_copy(update={"user_id": "other"})
    with pytest.raises(PermissionError):
        reader(intake, tmp_path, other).export_bytes(artifact)
    tampered = (tmp_path / f"artifacts/{artifact.task_id}/{artifact.artifact_id}.json").read_bytes()
    with pytest.raises(ValueError, match="xlsx_analysis_artifact_changed"):
        load_xlsx_analysis_artifact(tampered + b" ", artifact=artifact)


def test_binding_contracts_reject_unsorted_or_mismatched_intent():
    digest = "sha256:" + "0" * 64
    for columns in ((), (1, 0), (0, 0), (64,)):
        with pytest.raises(ValidationError):
            BindXlsxAnalysis(snapshot_hash=digest, columns=columns)
    with pytest.raises(ValidationError, match="xlsx_analysis_column_mapping_invalid"):
        XlsxAnalysisColumn(
            index=0,
            header="price",
            declared_type="decimal",
            analysis_type="integer",
            conversion="exact-integer",
        )


def test_binding_requires_an_explicitly_configured_port(tmp_path):
    intake = build_service(tmp_path)
    _, headers = credentials(intake.broker)
    client = client_for(intake)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = bind(client, headers, file_id, digest, (0,))
    assert response.status_code == 503
    assert response.json()["detail"] == "xlsx_analysis_not_configured"


def test_binder_state_must_stay_outside_the_artifact_tree(tmp_path):
    intake = build_service(tmp_path)
    budgets = TaskBudgetLedger(tmp_path / "budgets.db", clock=intake.store.clock)
    with pytest.raises(ValueError, match="xlsx_analysis_state_must_be_outside"):
        XlsxAnalysisBinder(
            intake=intake,
            budgets=budgets,
            artifact_root=tmp_path,
            artifact_index=tmp_path / "index.db",
        )
    with pytest.raises(ValueError, match="xlsx_analysis_state_must_be_outside"):
        XlsxAnalysisBinder(
            intake=intake,
            budgets=budgets,
            artifact_root=tmp_path / "artifacts",
            artifact_index=tmp_path / "artifacts" / "index.db",
        )


def test_app_rejects_a_binder_for_another_intake_or_without_intake(tmp_path):
    intake = build_service(tmp_path)
    other = build_service(tmp_path / "other")
    binder = XlsxAnalysisBinder(
        intake=other,
        budgets=TaskBudgetLedger(tmp_path / "budgets.db", clock=other.store.clock),
        artifact_root=tmp_path / "artifacts",
        artifact_index=tmp_path / "artifact-index.db",
    )
    with pytest.raises(ValueError, match="xlsx_analysis_must_share_intake"):
        create_app(session_broker=intake.broker, xlsx_intake=intake, xlsx_analysis=binder)
    with pytest.raises(ValueError, match="xlsx_analysis_must_share_intake"):
        create_app(session_broker=intake.broker, xlsx_analysis=binder)


@pytest.mark.parametrize("failure", ["audit", "revision", "revocation"])
def test_failed_binding_discards_only_its_new_artifact(tmp_path, monkeypatch, failure):
    intake, _, client, token, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    first = bind(client, headers, file_id, digest, (0,))
    assert first.status_code == 201
    existing = ArtifactRef.model_validate(first.json()["artifact"])
    artifacts = reader(intake, tmp_path)
    original = artifacts.read_bytes(existing)
    files = set((tmp_path / "artifacts").glob("*/*.json"))
    emitter = intake.audit.emit

    def fail_before_delivery(event):
        if event.event_type == "xlsx.analysis_dataset_bound":
            if failure == "audit":
                raise OSError("audit unavailable")
            if failure == "revision":
                revised = declaration_body(declared, columns=[COLUMNS[0]])
                assert declare(client, headers, file_id, revised).status_code == 200
            if failure == "revocation":
                intake.broker.revoke(token)
        emitter(event)

    monkeypatch.setattr(intake.audit, "emit", fail_before_delivery)
    response = bind(client, headers, file_id, digest, (0,))
    assert response.status_code == {"audit": 503, "revision": 409, "revocation": 404}[failure]
    assert set((tmp_path / "artifacts").glob("*/*.json")) == files
    assert artifacts.find_task_artifacts(existing.task_id, existing.artifact_type) == (existing,)
    assert artifacts.read_bytes(existing) == original
    with sqlite3.connect(tmp_path / "artifact-index.db") as db:
        deleted = db.execute(
            "SELECT deleted FROM governed_artifacts_v2 ORDER BY deleted"
        ).fetchall()
    assert deleted == [(0,), (1,)]


def test_values_page_returns_exact_typed_rows_and_advances_by_row_number(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    report = bind(client, headers, file_id, digest, (0, 1, 2, 3, 4)).json()
    artifact = report["artifact"]

    first = read_values(client, headers, file_id, artifact["artifact_id"], digest, limit=2)
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["reader"] == "gda-xlsx-analysis-values-v1"
    assert page["artifact_id"] == artifact["artifact_id"]
    assert page["artifact_hash"] == artifact["content_hash"]
    assert page["snapshot_hash"] == digest
    assert page["status"] == "current"
    assert page["values"] == "typed-exact-declared-columns-only"
    assert page["dlp"] == "artifact-rescanned-before-return"
    assert page["storage"] == "dlp-scanned-creator-artifact"
    assert page["row_scope"] == "present-value-only"
    assert page["roles"] == "not-assigned"
    assert page["target"] == "not-designated"
    assert page["dataset_snapshot"] is False
    assert page["certified_metric_source"] is False
    assert [column["analysis_type"] for column in page["columns"]] == [
        "string",
        "integer",
        "boolean",
        "string",
        "string",
    ]
    assert [(row["row"], row["values"]) for row in page["rows"]] == [
        (2, ["001", 2, True, "2.5", "2023-03-15"]),
        (3, ["002", 3, False, "-0.1", "2023-03-16"]),
    ]
    assert type(page["rows"][0]["values"][1]) is int
    assert type(page["rows"][0]["values"][2]) is bool
    assert (page["after"], page["more"], page["next"], page["retained_rows"]) == (None, True, 3, 3)

    second = read_values(
        client, headers, file_id, artifact["artifact_id"], digest, after=3, limit=2
    ).json()
    assert [row["row"] for row in second["rows"]] == [4]
    assert (second["after"], second["more"], second["next"]) == (3, False, None)
    assert "xlsx.analysis_values_read" in (tmp_path / "audit.jsonl").read_text()


def test_values_page_is_creator_scoped_and_expires(tmp_path):
    now = [datetime(2026, 11, 18, 1, 0, tzinfo=UTC)]
    intake, _, client, _, headers = setup(tmp_path, clock=lambda: now[0], retention=60)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0,)).json()["artifact"]["artifact_id"]
    assert read_values(client, headers, file_id, artifact_id, digest).status_code == 200
    other = actor().model_copy(update={"user_id": "other", "tenant_id": "tenant-2"})
    _, other_headers = credentials(intake.broker, other)
    assert read_values(client, other_headers, file_id, artifact_id, digest).status_code == 404
    now[0] += timedelta(seconds=61)
    assert read_values(client, headers, file_id, artifact_id, digest).status_code == 404


def test_values_page_requires_the_exact_current_snapshot_and_task(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0,)).json()["artifact"]["artifact_id"]

    unknown = read_values(client, headers, file_id, UUID(int=0), digest)
    assert unknown.status_code == 404
    changed = read_values(client, headers, file_id, artifact_id, "sha256:" + "0" * 64)
    assert changed.status_code == 409
    assert changed.json()["detail"] == "xlsx_snapshot_changed"

    second = client.post(
        "/v1/analysis/files/xlsx",
        headers=headers | {"content-type": XLSX, "idempotency-key": "xlsx-2"},
        content=RAW,
    ).json()
    other_file = second["file"]["file_id"]
    confirmed = confirm(client, headers, other_file, confirm_body(second)).json()
    assert (
        declare(
            client, headers, other_file, declaration_body(confirmed, columns=COLUMNS)
        ).status_code
        == 200
    )
    other_digest = snapshot(client, headers, other_file).json()["snapshot_hash"]
    assert read_values(client, headers, other_file, artifact_id, other_digest).status_code == 404

    response = declare(client, headers, file_id, declaration_body(declared, columns=[COLUMNS[0]]))
    assert response.status_code == 200, response.text
    stale = read_values(client, headers, file_id, artifact_id, digest)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "xlsx_snapshot_stale"
    new_digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    old_artifact = read_values(client, headers, file_id, artifact_id, new_digest)
    assert old_artifact.status_code == 409
    assert old_artifact.json()["detail"] == "xlsx_snapshot_changed"


def test_values_page_rescans_the_artifact_before_return(tmp_path):
    class LateScanner:
        def __init__(self):
            self.deny = False

        def check(self, content: bytes) -> None:
            if self.deny and b"2023-03-15" in content:
                raise DLPDeniedError("dlp_personal_data")

    scanner = LateScanner()
    _, _, client, _, headers = setup(tmp_path, scanner=scanner)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 3, 4)).json()["artifact"][
        "artifact_id"
    ]
    scanner.deny = True
    response = read_values(client, headers, file_id, artifact_id, digest)
    assert response.status_code == 404
    assert response.json()["detail"] == "xlsx_file_unavailable"
    assert "xlsx.analysis_values_read" not in (tmp_path / "audit.jsonl").read_text()


def test_values_page_contracts_enforce_types_order_and_the_character_budget():
    column = XlsxAnalysisColumn(
        index=0,
        header="label",
        declared_type="text",
        analysis_type="string",
        conversion="exact-text",
    )
    digest = "sha256:" + "0" * 64
    large = XlsxAnalysisArtifact(
        file_id=UUID(int=1),
        task_id=UUID(int=2),
        source_hash=digest,
        preview_hash=digest,
        confirmation_hash=digest,
        interpretation_hash=digest,
        snapshot_hash=digest,
        date_system="1900",
        duplicate_rows="reject",
        duplicate_row_count=0,
        columns=(column,),
        row_numbers=tuple(range(1, 201)),
        rows=tuple(("x" * 12000,) for _ in range(200)),
    )
    with pytest.raises(XlsxAnalysisRefused) as limit:
        analysis_page(large, artifact_id=UUID(int=3), artifact_hash=digest, limit=0)
    assert limit.value.code == "xlsx_analysis_page_limit_invalid"
    with pytest.raises(XlsxAnalysisRefused) as characters:
        analysis_page(large, artifact_id=UUID(int=3), artifact_hash=digest, limit=500)
    assert characters.value.code == "xlsx_analysis_page_char_limit"

    small = large.model_copy(update={"row_numbers": (1, 2), "rows": (("a",), ("b",))})
    page = analysis_page(small, artifact_id=UUID(int=3), artifact_hash=digest)
    for rows in (
        ({"schema_version": "1.0", "row": 2, "values": [2]},),
        (
            {"schema_version": "1.0", "row": 2, "values": ["a"]},
            {"schema_version": "1.0", "row": 1, "values": ["b"]},
        ),
        ({"schema_version": "1.0", "row": 1, "values": ["a", "extra"]},),
    ):
        with pytest.raises(ValidationError, match="xlsx_analysis_page_invalid"):
            XlsxAnalysisValuePage.model_validate(page.model_dump() | {"rows": rows})


def test_artifact_lookup_is_exact_and_creator_scoped(tmp_path):
    intake, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact = ArtifactRef.model_validate(
        bind(client, headers, file_id, digest, (0,)).json()["artifact"]
    )
    store = reader(intake, tmp_path)
    assert (
        store.find_task_artifact(artifact.task_id, artifact.artifact_id, "xlsx_analysis_dataset")
        == artifact
    )
    assert store.find_task_artifact(artifact.task_id, artifact.artifact_id, "other_type") is None
    assert (
        store.find_task_artifact(UUID(int=0), artifact.artifact_id, "xlsx_analysis_dataset") is None
    )
    other = actor().model_copy(update={"user_id": "other"})
    assert (
        reader(intake, tmp_path, other).find_task_artifact(
            artifact.task_id, artifact.artifact_id, "xlsx_analysis_dataset"
        )
        is None
    )
    with pytest.raises(ValueError, match="artifact lookup requires"):
        store.find_task_artifact(artifact.task_id, artifact.artifact_id, "")


def read_profile(client, headers, file_id, artifact_id, digest):
    return client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/analysis/{artifact_id}/profile",
        headers=headers,
        params={"snapshot_hash": digest},
    )


def test_profile_returns_exact_counts_and_integer_extremes_without_values(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact = bind(client, headers, file_id, digest, (0, 1, 2, 3, 4)).json()["artifact"]

    response = read_profile(client, headers, file_id, artifact["artifact_id"], digest)
    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["reader"] == "gda-xlsx-analysis-profile-v1"
    assert profile["artifact_id"] == artifact["artifact_id"]
    assert profile["artifact_hash"] == artifact["content_hash"]
    assert profile["snapshot_hash"] == digest
    assert profile["status"] == "current"
    assert profile["scope"] == "selected-declared-columns"
    assert profile["aggregation"] == "exact-counts-and-integer-extremes"
    assert profile["values"] == "counts-only-not-returned"
    assert profile["dlp"] == "header-text-rescanned-before-return"
    assert profile["storage"] == "dlp-scanned-creator-artifact"
    assert profile["row_scope"] == "present-value-only"
    assert profile["roles"] == "not-assigned"
    assert profile["target"] == "not-designated"
    assert profile["dataset_snapshot"] is False
    assert profile["certified_metric_source"] is False
    assert (profile["row_count"], profile["cells"]) == (3, 15)
    assert [column["index"] for column in profile["columns"]] == [0, 1, 2, 3, 4]
    assert [column["analysis_type"] for column in profile["columns"]] == [
        "string",
        "integer",
        "boolean",
        "string",
        "string",
    ]
    assert [column["present_count"] for column in profile["columns"]] == [3, 3, 3, 3, 3]
    assert [column["distinct_count"] for column in profile["columns"]] == [3, 3, 2, 3, 3]
    count = profile["columns"][1]
    assert (count["integer_min"], count["integer_max"]) == (-4, 3)
    assert all(
        column["integer_min"] is None and column["integer_max"] is None
        for column in profile["columns"]
        if column["analysis_type"] != "integer"
    )
    body = json.dumps(profile)
    # Match quoted JSON strings, not bare digit runs: task/artifact ids and hashes are random
    # UUIDs/hex and can legitimately contain substrings such as "001".
    for value in ('"001"', "2023-03-15", '"2.5"', '"10"'):
        assert value not in body
    assert [column["header"] for column in profile["columns"]] == list(HEADERS)
    assert "xlsx.analysis_profile_read" in (tmp_path / "audit.jsonl").read_text()


def test_profile_projects_only_the_bound_columns(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact = bind(client, headers, file_id, digest, (1, 4)).json()["artifact"]
    profile = read_profile(client, headers, file_id, artifact["artifact_id"], digest).json()
    assert [column["index"] for column in profile["columns"]] == [1, 4]
    assert [column["header"] for column in profile["columns"]] == ["count", "day"]
    assert (profile["row_count"], profile["cells"]) == (3, 6)


def test_profile_is_creator_scoped_and_expires(tmp_path):
    now = [datetime(2026, 11, 18, 1, 0, tzinfo=UTC)]
    intake, _, client, _, headers = setup(tmp_path, clock=lambda: now[0], retention=60)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0,)).json()["artifact"]["artifact_id"]
    assert read_profile(client, headers, file_id, artifact_id, digest).status_code == 200
    other = actor().model_copy(update={"user_id": "other", "tenant_id": "tenant-2"})
    _, other_headers = credentials(intake.broker, other)
    assert read_profile(client, other_headers, file_id, artifact_id, digest).status_code == 404
    now[0] += timedelta(seconds=61)
    assert read_profile(client, headers, file_id, artifact_id, digest).status_code == 404


def test_profile_requires_the_exact_current_snapshot_and_task(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0,)).json()["artifact"]["artifact_id"]

    unknown = read_profile(client, headers, file_id, UUID(int=0), digest)
    assert unknown.status_code == 404
    changed = read_profile(client, headers, file_id, artifact_id, "sha256:" + "0" * 64)
    assert changed.status_code == 409
    assert changed.json()["detail"] == "xlsx_snapshot_changed"

    revised = declaration_body(declared, columns=[COLUMNS[0]])
    assert declare(client, headers, file_id, revised).status_code == 200
    stale = read_profile(client, headers, file_id, artifact_id, digest)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "xlsx_snapshot_stale"
    new_digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    old_artifact = read_profile(client, headers, file_id, artifact_id, new_digest)
    assert old_artifact.status_code == 409
    assert old_artifact.json()["detail"] == "xlsx_snapshot_changed"


def test_profile_rescans_headers_before_return(tmp_path):
    class LateScanner:
        def __init__(self):
            self.deny = False

        def check(self, content: bytes) -> None:
            if self.deny and b"price" in content:
                raise DLPDeniedError("dlp_personal_data")

    scanner = LateScanner()
    _, _, client, _, headers = setup(tmp_path, scanner=scanner)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 3)).json()["artifact"]["artifact_id"]
    scanner.deny = True
    response = read_profile(client, headers, file_id, artifact_id, digest)
    assert response.status_code == 404
    assert response.json()["detail"] == "xlsx_file_unavailable"
    assert "xlsx.analysis_profile_read" not in (tmp_path / "audit.jsonl").read_text()


def test_profile_contracts_reject_incoherent_counts_and_extremes():
    digest = "sha256:" + "0" * 64
    profile = XlsxAnalysisProfile(
        file_id=UUID(int=1),
        task_id=UUID(int=2),
        artifact_id=UUID(int=3),
        artifact_hash=digest,
        snapshot_hash=digest,
        row_count=2,
        cells=2,
        columns=(
            XlsxAnalysisColumnProfile(
                index=0,
                header="count",
                declared_type="integer",
                analysis_type="integer",
                conversion="exact-integer",
                present_count=2,
                distinct_count=1,
                integer_min=1,
                integer_max=1,
            ),
        ),
    )
    assert profile.columns[0].integer_min == 1
    for update in (
        {"cells": 3},
        {"row_count": 3},
        {"columns": (profile.columns[0].model_copy(update={"present_count": 1}),)},
    ):
        with pytest.raises(ValidationError, match="xlsx_analysis_profile_invalid"):
            XlsxAnalysisProfile.model_validate(profile.model_dump() | update)
    for update in (
        {"distinct_count": 3},
        {"integer_min": None},
        {"integer_min": 2},
        {"header": ""},
        {"analysis_type": "string", "conversion": "exact-text"},
    ):
        with pytest.raises(ValidationError, match="xlsx_analysis_profile_invalid"):
            XlsxAnalysisColumnProfile.model_validate(profile.columns[0].model_dump() | update)


def test_profile_requires_an_explicitly_configured_port(tmp_path):
    intake = build_service(tmp_path)
    _, headers = credentials(intake.broker)
    client = client_for(intake)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = read_profile(client, headers, file_id, UUID(int=0), digest)
    assert response.status_code == 503
    assert response.json()["detail"] == "xlsx_analysis_not_configured"


def read_metric(client, headers, file_id, artifact_id, digest, metric, column=None):
    params = {"snapshot_hash": digest, "metric": metric}
    if column is not None:
        params["column"] = column
    return client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/analysis/{artifact_id}/metric",
        headers=headers,
        params=params,
    )


def test_metric_view_returns_exact_integer_aggregates_without_cell_values(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    report = bind(client, headers, file_id, digest, (0, 1, 2, 3, 4)).json()
    artifact_id = report["artifact"]["artifact_id"]

    rows = read_metric(client, headers, file_id, artifact_id, digest, "row-count")
    assert rows.status_code == 200, rows.text
    body = rows.json()
    assert body["reader"] == "gda-xlsx-analysis-metric-v1"
    assert body["metric"] == "row-count"
    assert body["column"] is None
    assert body["value"] == 3
    assert body["row_count"] == 3
    assert body["dlp"] == "no-text-disclosed"

    expected = {
        ("distinct-count", 0): 3,
        ("distinct-count", 1): 3,
        ("distinct-count", 2): 2,
        ("distinct-count", 3): 3,
        ("distinct-count", 4): 3,
        ("integer-sum", 1): 1,
        ("integer-min", 1): -4,
        ("integer-max", 1): 3,
    }
    for (metric, column), value in expected.items():
        response = read_metric(client, headers, file_id, artifact_id, digest, metric, column)
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["value"] == value
        assert view["column"]["index"] == column
        assert view["dlp"] == "header-text-rescanned-before-return"
        assert view["scope"] == "one-creator-bound-analysis-artifact"
        assert view["aggregation"] == "exact-integer-single-pass"
        assert view["qualification"] == "non-certified-analysis-view"
        assert view["metric_catalog"] == "not-registered"
        assert view["metric_definition"] == "not-resolved"
        assert view["result_view"] == "analysis-metric-view-not-metric-result"
        assert view["storage"] == "dlp-scanned-creator-artifact"
        assert view["row_scope"] == "present-value-only"
        assert view["roles"] == "not-assigned"
        assert view["target"] == "not-designated"
        assert view["dataset_snapshot"] is False
        assert view["certified_metric_source"] is False
        assert view["artifact_id"] == artifact_id
        assert view["artifact_hash"] == report["artifact"]["content_hash"]
        assert view["snapshot_hash"] == digest
        assert view["status"] == "current"
        assert "rows" not in view and "values" not in view
        text = json.dumps(view)
        for leaked in ("2.5", "-0.1", "2023-03-15"):
            assert leaked not in text

    events = (tmp_path / "audit.jsonl").read_text()
    assert "xlsx.analysis_metric_read" in events


def test_metric_view_refuses_non_integer_extremes_and_unbound_columns(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 1, 3)).json()["artifact"][
        "artifact_id"
    ]

    for metric in ("integer-sum", "integer-min", "integer-max"):
        refused = read_metric(client, headers, file_id, artifact_id, digest, metric, 0)
        assert refused.status_code == 422
        assert refused.json()["detail"] == "xlsx_analysis_metric_type_invalid"
        decimal = read_metric(client, headers, file_id, artifact_id, digest, metric, 3)
        assert decimal.status_code == 422
        assert decimal.json()["detail"] == "xlsx_analysis_metric_type_invalid"

    unbound = read_metric(client, headers, file_id, artifact_id, digest, "distinct-count", 2)
    assert unbound.status_code == 422
    assert unbound.json()["detail"] == "xlsx_analysis_metric_column_not_bound"

    for params in (
        {"metric": "row-count", "column": 1},
        {"metric": "integer-sum"},
    ):
        response = client.get(
            f"/v1/analysis/files/xlsx/{file_id}/snapshot/analysis/{artifact_id}/metric",
            headers=headers,
            params={"snapshot_hash": digest, **params},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "xlsx_input_invalid"

    assert "xlsx.analysis_metric_read" not in (tmp_path / "audit.jsonl").read_text()


def test_metric_view_is_creator_scoped_and_expires(tmp_path):
    now = [datetime(2026, 11, 19, 1, 0, tzinfo=UTC)]
    intake, _, client, _, headers = setup(tmp_path, clock=lambda: now[0], retention=60)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 1)).json()["artifact"]["artifact_id"]
    assert (
        read_metric(client, headers, file_id, artifact_id, digest, "integer-sum", 1).status_code
        == 200
    )
    other = actor().model_copy(update={"user_id": "other", "tenant_id": "tenant-2"})
    _, other_headers = credentials(intake.broker, other)
    assert (
        read_metric(
            client, other_headers, file_id, artifact_id, digest, "integer-sum", 1
        ).status_code
        == 404
    )
    now[0] += timedelta(seconds=61)
    assert (
        read_metric(client, headers, file_id, artifact_id, digest, "integer-sum", 1).status_code
        == 404
    )


def test_metric_view_requires_the_exact_current_snapshot_and_task(tmp_path):
    _, _, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 1)).json()["artifact"]["artifact_id"]

    unknown = read_metric(client, headers, file_id, UUID(int=0), digest, "row-count")
    assert unknown.status_code == 404
    changed = read_metric(client, headers, file_id, artifact_id, "sha256:" + "0" * 64, "row-count")
    assert changed.status_code == 409
    assert changed.json()["detail"] == "xlsx_snapshot_changed"

    second = client.post(
        "/v1/analysis/files/xlsx",
        headers=headers | {"content-type": XLSX, "idempotency-key": "xlsx-metric-2"},
        content=RAW,
    ).json()
    other_file = second["file"]["file_id"]
    confirmed = confirm(client, headers, other_file, confirm_body(second)).json()
    assert (
        declare(
            client, headers, other_file, declaration_body(confirmed, columns=COLUMNS)
        ).status_code
        == 200
    )
    other_digest = snapshot(client, headers, other_file).json()["snapshot_hash"]
    assert (
        read_metric(client, headers, other_file, artifact_id, other_digest, "row-count").status_code
        == 404
    )

    response = declare(client, headers, file_id, declaration_body(declared, columns=[COLUMNS[0]]))
    assert response.status_code == 200, response.text
    stale = read_metric(client, headers, file_id, artifact_id, digest, "row-count")
    assert stale.status_code == 409
    assert stale.json()["detail"] == "xlsx_snapshot_stale"
    new_digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    old_artifact = read_metric(client, headers, file_id, artifact_id, new_digest, "row-count")
    assert old_artifact.status_code == 409
    assert old_artifact.json()["detail"] == "xlsx_snapshot_changed"


def test_metric_view_rescans_the_disclosed_header_before_return(tmp_path):
    class HeaderScanner:
        def __init__(self):
            self.deny = False

        def check(self, content: bytes) -> None:
            if self.deny and content == b"count":
                raise DLPDeniedError("dlp_personal_data")

    scanner = HeaderScanner()
    _, _, client, _, headers = setup(tmp_path, scanner=scanner)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 1)).json()["artifact"]["artifact_id"]
    scanner.deny = True

    denied = read_metric(client, headers, file_id, artifact_id, digest, "integer-sum", 1)
    assert denied.status_code == 404
    assert denied.json()["detail"] == "xlsx_file_unavailable"
    assert "xlsx.analysis_metric_read" not in (tmp_path / "audit.jsonl").read_text()

    rows = read_metric(client, headers, file_id, artifact_id, digest, "row-count")
    assert rows.status_code == 200, rows.text
    assert rows.json()["value"] == 3
    assert rows.json()["dlp"] == "no-text-disclosed"


@pytest.mark.parametrize("failure", ["audit", "revision"])
def test_failed_metric_view_does_not_return_a_value(tmp_path, monkeypatch, failure):
    intake, _, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    artifact_id = bind(client, headers, file_id, digest, (0, 1)).json()["artifact"]["artifact_id"]
    emitter = intake.audit.emit

    def fail_before_delivery(event):
        if event.event_type == "xlsx.analysis_metric_read":
            if failure == "audit":
                raise OSError("audit unavailable")
            revised = declaration_body(declared, columns=[COLUMNS[0]])
            assert declare(client, headers, file_id, revised).status_code == 200
        emitter(event)

    monkeypatch.setattr(intake.audit, "emit", fail_before_delivery)
    response = read_metric(client, headers, file_id, artifact_id, digest, "integer-sum", 1)
    assert response.status_code == {"audit": 503, "revision": 409}[failure]
    assert "value" not in response.json()


def test_metric_view_is_charged_to_the_task_budget(tmp_path):
    _, binder, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    report = bind(client, headers, file_id, digest, (0, 1)).json()
    artifact_id = report["artifact"]["artifact_id"]
    task_id = UUID(report["task_id"])

    first = read_metric(client, headers, file_id, artifact_id, digest, "row-count")
    assert first.status_code == 200, first.text
    for _ in range(29):
        binder.budgets.reserve(task_id, actor(), scan_bytes=0)

    denied = read_metric(client, headers, file_id, artifact_id, digest, "row-count")
    assert denied.status_code == 429
    assert denied.json()["detail"] == "xlsx_budget_exceeded"
    events = (tmp_path / "audit.jsonl").read_text()
    assert events.count("xlsx.analysis_metric_read") == 1


def test_metric_contracts_reject_incoherent_columns_and_values():
    digest = "sha256:" + "0" * 64
    integer = XlsxAnalysisColumn(
        index=1,
        header="count",
        declared_type="integer",
        analysis_type="integer",
        conversion="exact-integer",
    )
    decimal = XlsxAnalysisColumn(
        index=3,
        header="price",
        declared_type="decimal",
        analysis_type="string",
        conversion="exact-decimal-text",
    )
    for request in (
        {"metric": "row-count", "column": 1},
        {"metric": "distinct-count"},
    ):
        with pytest.raises(ValidationError, match="xlsx_analysis_metric_column_invalid"):
            ReadXlsxAnalysisMetric(snapshot_hash=digest, **request)

    base = {
        "file_id": UUID(int=1),
        "task_id": UUID(int=2),
        "artifact_id": UUID(int=3),
        "artifact_hash": digest,
        "snapshot_hash": digest,
        "metric": "distinct-count",
        "column": integer,
        "value": 1,
        "row_count": 2,
        "dlp": "header-text-rescanned-before-return",
    }
    assert XlsxAnalysisMetric(**base).value == 1
    for update in (
        {"value": 0},
        {"metric": "row-count"},
        {"column": None},
        {"dlp": "no-text-disclosed"},
        {"metric": "integer-sum", "column": decimal, "value": 1},
        {"metric": "row-count", "column": None, "value": 1},
    ):
        with pytest.raises(ValidationError, match="xlsx_analysis_metric_invalid"):
            XlsxAnalysisMetric.model_validate(base | update)


def test_metric_view_requires_an_explicitly_configured_port(tmp_path):
    intake = build_service(tmp_path)
    _, headers = credentials(intake.broker)
    client = client_for(intake)
    file_id, _ = prepared(client, headers, raw=RAW, columns=COLUMNS)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    response = read_metric(client, headers, file_id, UUID(int=0), digest, "row-count")
    assert response.status_code == 503
    assert response.json()["detail"] == "xlsx_analysis_not_configured"
