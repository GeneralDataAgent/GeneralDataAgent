import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_metric_analysis import actor
from test_stage13_xlsx_conformance import prepared
from test_stage13_xlsx_quality import read_receipt, request_quality, setup
from test_stage13_xlsx_snapshot import snapshot

from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.datasets.xlsx_quality import XlsxQualityService
from generaldataagent.datasets.xlsx_quality_intents import XlsxQualityIntents

INTENT = "quality-hard-interrupt"


def _crash(*_args, **_kwargs):
    raise RuntimeError("simulated hard interrupt")


def test_recovers_written_evidence_after_the_claim_lease(tmp_path, monkeypatch):
    now = [datetime.now(UTC)]
    intake, client, _, headers = setup(tmp_path, clock=lambda: now[0])
    file_id, _ = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]

    with monkeypatch.context() as patch:
        patch.setattr(XlsxQualityIntents, "settle", _crash)
        interrupted = request_quality(client, headers, file_id, digest, intent=INTENT)
    assert interrupted.status_code == 503
    pending = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest).json()
    assert pending["state"] == "pending"
    assert pending["result"] is None
    assert request_quality(client, headers, file_id, digest, intent=INTENT).status_code == 409

    now[0] += timedelta(seconds=intake.limits.max_seconds + 31)
    recovered = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest).json()
    assert recovered["state"] == "completed"
    assert recovered["task_id"] == pending["task_id"]
    assert recovered["result"]["assessment"]["task_id"] == pending["task_id"]
    assert recovered["result"]["evidence"]["artifact_type"] == "xlsx_snapshot_quality"
    assert len(list((tmp_path / "artifacts").glob("*/*.json"))) == 1
    assert (
        request_quality(client, headers, file_id, digest, intent=INTENT).json()
        == recovered["result"]
    )
    events = [line for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert '"outcome":"completed"' in "\n".join(events)


def test_failed_claim_can_be_replaced_after_the_lease(tmp_path, monkeypatch):
    now = [datetime.now(UTC)]
    intake, client, _, headers = setup(tmp_path, clock=lambda: now[0])
    file_id, _ = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]

    with monkeypatch.context() as patch:
        patch.setattr(XlsxQualityService, "_assess", _crash)
        patch.setattr(XlsxQualityIntents, "settle", _crash)
        interrupted = request_quality(client, headers, file_id, digest, intent=INTENT)
    assert interrupted.status_code == 503
    pending = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest).json()
    assert pending["state"] == "pending"
    assert list((tmp_path / "artifacts").glob("*/*.json")) == []

    now[0] += timedelta(seconds=intake.limits.max_seconds + 31)
    failed = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest).json()
    assert failed["state"] == "failed"
    assert failed["task_id"] == pending["task_id"]
    assert request_quality(client, headers, file_id, digest, intent=INTENT).status_code == 409

    retried = request_quality(client, headers, file_id, digest, intent="quality-retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["assessment"]["task_id"] != pending["task_id"]


def test_recovery_refuses_ambiguous_evidence_and_keeps_the_claim(tmp_path, monkeypatch):
    now = [datetime.now(UTC)]
    intake, client, _, headers = setup(tmp_path, clock=lambda: now[0])
    file_id, _ = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]

    with monkeypatch.context() as patch:
        patch.setattr(XlsxQualityIntents, "settle", _crash)
        interrupted = request_quality(client, headers, file_id, digest, intent=INTENT)
    assert interrupted.status_code == 503
    pending = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest).json()
    store = GovernedArtifactStore(
        tmp_path / "artifacts",
        index_path=tmp_path / "artifact-index.db",
        actor=actor(),
        scanner=intake.scanner,
        retention_seconds=intake.retention_seconds,
        clock=intake.store.clock,
        read_policy=ArtifactReadPolicy.CREATOR,
    )
    for _ in range(2):
        store.put_json(
            task_id=pending["task_id"],
            artifact_type="xlsx_snapshot_quality",
            value={},
            created_by=actor().user_id,
        )

    now[0] += timedelta(seconds=intake.limits.max_seconds + 31)
    ambiguous = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest)
    assert ambiguous.status_code == 503
    assert ambiguous.json()["detail"] == "xlsx_intake_unavailable"


def test_recovery_stays_pending_when_audit_is_unavailable(tmp_path, monkeypatch):
    now = [datetime.now(UTC)]
    intake, client, _, headers = setup(tmp_path, clock=lambda: now[0])
    file_id, _ = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]

    with monkeypatch.context() as patch:
        patch.setattr(XlsxQualityIntents, "settle", _crash)
        interrupted = request_quality(client, headers, file_id, digest, intent=INTENT)
    assert interrupted.status_code == 503
    now[0] += timedelta(seconds=intake.limits.max_seconds + 31)
    emit = intake.audit.emit

    def fail_recovery(event):
        if event.event_type == "xlsx.snapshot_quality_intent_recovered":
            raise OSError("audit unavailable")
        emit(event)

    monkeypatch.setattr(intake.audit, "emit", fail_recovery)
    unavailable = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest)
    assert unavailable.status_code == 503
    monkeypatch.setattr(intake.audit, "emit", emit)
    recovered = read_receipt(client, {**headers, "idempotency-key": INTENT}, file_id, digest)
    assert recovered.json()["state"] == "completed"


def test_task_artifact_lookup_is_creator_scoped_and_excludes_gone_records(tmp_path):
    now = [datetime.now(UTC)]
    root, index = tmp_path / "artifacts", tmp_path / "artifact-index.db"

    def store(user="synthetic", retention=60):
        return GovernedArtifactStore(
            root,
            index_path=index,
            actor=actor().model_copy(update={"user_id": user}),
            retention_seconds=retention,
            clock=lambda: now[0],
            read_policy=ArtifactReadPolicy.CREATOR,
        )

    task_id = uuid4()
    writer = store()
    ref = writer.put_json(
        task_id=task_id,
        artifact_type="xlsx_snapshot_quality",
        value={"synthetic": True},
        created_by="synthetic",
    )
    assert writer.find_task_artifacts(task_id, "xlsx_snapshot_quality") == (ref,)
    assert writer.find_task_artifacts(task_id, "quality_report") == ()
    assert store("other").find_task_artifacts(task_id, "xlsx_snapshot_quality") == ()

    assert writer.discard(ref) is True
    assert writer.find_task_artifacts(task_id, "xlsx_snapshot_quality") == ()


def test_task_artifact_lookup_excludes_expired_records(tmp_path):
    now = [datetime.now(UTC)]
    writer = GovernedArtifactStore(
        tmp_path / "artifacts",
        index_path=tmp_path / "artifact-index.db",
        actor=actor(),
        retention_seconds=1,
        clock=lambda: now[0],
        read_policy=ArtifactReadPolicy.CREATOR,
    )
    task_id = uuid4()
    writer.put_json(
        task_id=task_id,
        artifact_type="xlsx_snapshot_quality",
        value={"synthetic": True},
        created_by=actor().user_id,
    )
    now[0] += timedelta(seconds=2)
    assert writer.find_task_artifacts(task_id, "xlsx_snapshot_quality") == ()


def test_legacy_intent_rows_gain_a_recoverable_lease(tmp_path):
    index = tmp_path / "artifact-index.db"
    with sqlite3.connect(index) as db:
        db.execute(
            "CREATE TABLE xlsx_quality_intents ("
            "scope TEXT NOT NULL, intent TEXT NOT NULL, file_id TEXT NOT NULL, "
            "snapshot_hash TEXT NOT NULL, task_id TEXT NOT NULL, expires REAL NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN ('pending','failed','completed')), "
            "evidence TEXT, PRIMARY KEY(scope,intent))"
        )
        db.execute(
            "INSERT INTO xlsx_quality_intents VALUES ('scope','intent',?,?,?,?, 'pending', NULL)",
            (str(uuid4()), "sha256:" + "0" * 64, str(uuid4()), datetime.now(UTC).timestamp()),
        )
    intents = XlsxQualityIntents(index)
    with sqlite3.connect(index) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(xlsx_quality_intents)")}
    assert "lease_until" in columns
    with sqlite3.connect(index) as db:
        scope, intent, file_id, digest = db.execute(
            "SELECT scope, intent, file_id, snapshot_hash FROM xlsx_quality_intents"
        ).fetchone()
    claim = intents.lookup(scope, intent, file_id, digest)
    assert claim is not None
    assert claim.state == "pending"
    assert claim.lease_until == 0
