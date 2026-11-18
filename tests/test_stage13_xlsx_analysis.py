import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import prepared
from test_stage13_xlsx_intake import (
    build_service,
    client_for,
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
    XlsxAnalysisBinder,
    XlsxAnalysisColumn,
    XlsxAnalysisRefused,
    _exact_value,
    load_xlsx_analysis_artifact,
)
from generaldataagent.datasets.xlsx_intake_contracts import XlsxSnapshotColumnData

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
    with pytest.raises(ValueError, match="xlsx_analysis_state_must_be_outside"):
        XlsxAnalysisBinder(
            intake=intake, artifact_root=tmp_path, artifact_index=tmp_path / "index.db"
        )
    with pytest.raises(ValueError, match="xlsx_analysis_state_must_be_outside"):
        XlsxAnalysisBinder(
            intake=intake,
            artifact_root=tmp_path / "artifacts",
            artifact_index=tmp_path / "artifacts" / "index.db",
        )


def test_app_rejects_a_binder_for_another_intake_or_without_intake(tmp_path):
    intake = build_service(tmp_path)
    other = build_service(tmp_path / "other")
    binder = XlsxAnalysisBinder(
        intake=other,
        artifact_root=tmp_path / "artifacts",
        artifact_index=tmp_path / "artifact-index.db",
    )
    with pytest.raises(ValueError, match="xlsx_analysis_must_share_intake"):
        create_app(session_broker=intake.broker, xlsx_intake=intake, xlsx_analysis=binder)
    with pytest.raises(ValueError, match="xlsx_analysis_must_share_intake"):
        create_app(session_broker=intake.broker, xlsx_analysis=binder)
