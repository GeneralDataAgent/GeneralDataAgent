import sqlite3
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from test_stage5_actions import HASH
from test_stage5_actions import setup as action_fixture

from generaldataagent.business.schedules import ReportSchedule, ReportScheduler
from generaldataagent.business.worker import ReportWorker
from generaldataagent.contracts.business import ReportDraft


@pytest.fixture
def setup(tmp_path):
    return action_fixture.__wrapped__(tmp_path)


def prepare(tmp_path, setup):
    actions, approvals, broker, owner = setup
    now = [datetime.now(UTC)]
    actions.clock = lambda: now[0]
    broker._clock = lambda: now[0]
    scheduler = ReportScheduler(tmp_path / "schedules", actions=actions)
    spec = ReportSchedule(
        schedule_id=uuid4(),
        destination="report",
        report=ReportDraft(title="Report", report_hash=HASH, period="template"),
        starts_at=now[0] + timedelta(seconds=1),
        interval_minutes=60,
        occurrences=2,
    )
    scheduler.register(spec, owner)
    issued = []

    def provider(workload):
        token = broker.issue(
            user_id=workload.user_id,
            tenant_id=workload.tenant_id,
            project_id=workload.project_id,
            data_domains=frozenset(workload.data_domains),
            permissions=frozenset({"business:propose"}),
            not_after=workload.expires_at,
        )
        issued.append(token)
        return token

    worker = ReportWorker(tmp_path / "worker", scheduler=scheduler, token_provider=provider)
    worker.register(spec.schedule_id, owner, expires_at=now[0] + timedelta(hours=3))
    now[0] += timedelta(seconds=2)
    return worker, spec, now, owner, issued


def test_poll_restart_new_sessions_and_approvals_only(tmp_path, setup):
    worker, spec, now, _, issued = prepare(tmp_path, setup)
    assert worker.run_once()[0]["state"] == "active"
    assert worker.run_once() == []
    with pytest.raises(PermissionError):
        worker.scheduler.actions.broker.authenticate(issued[0])
    restarted = ReportWorker(
        worker.path, scheduler=worker.scheduler, token_provider=worker.token_provider
    )
    now[0] += timedelta(hours=1)
    assert restarted.run_once()[0]["state"] == "complete"
    assert restarted.run_once() == []
    with worker.scheduler.actions._db() as db:
        rows = db.execute("SELECT state FROM business_actions").fetchall()
    assert len(rows) == 2
    assert all(row[0] != "completed" for row in rows)
    assert len(set(issued)) == 2
    assert all(token not in worker.path.read_bytes().decode(errors="ignore") for token in issued)


@pytest.mark.parametrize(
    "mismatch", ["user_id", "project_id", "data_domains", "permissions", "expiry"]
)
def test_wrong_scope_privileged_or_overlong_session_denied(tmp_path, setup, mismatch):
    worker, _, _, _, _ = prepare(tmp_path, setup)
    broker = worker.scheduler.actions.broker

    def provider(w):
        values = dict(
            user_id=w.user_id,
            tenant_id=w.tenant_id,
            project_id=w.project_id,
            data_domains=frozenset(w.data_domains),
            permissions=frozenset({"business:propose"}),
            not_after=w.expires_at,
        )
        if mismatch in {"user_id", "project_id"}:
            values[mismatch] = "outsider"
        elif mismatch == "data_domains":
            values[mismatch] = frozenset({"sales", "private"})
        elif mismatch == "permissions":
            values[mismatch] = frozenset({"business:propose", "actions:resume"})
        else:
            # Modify the delegated expiry to less than the fresh session lifetime.
            values["not_after"] = None
            values["ttl_seconds"] = 900
        return broker.issue(**values)

    if mismatch == "expiry":
        with worker._db() as db, db:
            row = db.execute("SELECT workload FROM report_jobs").fetchone()[0]
            from generaldataagent.business.worker import ScheduleWorkload

            w = ScheduleWorkload.model_validate_json(row)
            w = w.model_copy(
                update={"expires_at": worker.scheduler.actions._now() + timedelta(seconds=20)}
            )
            db.execute("UPDATE report_jobs SET workload=?", (w.model_dump_json(),))
    worker.token_provider = provider
    assert worker.run_once()[0]["state"] == "failed"
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT count(*) FROM business_actions").fetchone()[0] == 0


def test_lease_exclusion_crash_recovery_expiry_and_redaction(tmp_path, setup):
    worker, _, now, _, _ = prepare(tmp_path, setup)
    with worker._db() as db, db:
        db.execute(
            "UPDATE report_jobs SET lease='dead-worker',lease_until=?", (now[0].timestamp() + 60,)
        )
    assert worker.run_once() == []
    now[0] += timedelta(seconds=61)
    worker.token_provider = lambda w: (_ for _ in ()).throw(ConnectionError("SECRET"))
    result = worker.run_once()
    assert result[0]["error"] == "ConnectionError"
    assert "SECRET" not in str(result)
    now[0] += timedelta(hours=4)
    assert worker.run_once()[0]["state"] == "failed"
    assert worker.run_once() == []


def test_cancel_during_provider_and_stop_loop(tmp_path, setup):
    worker, spec, _, owner, _ = prepare(tmp_path, setup)
    original = worker.token_provider

    def cancel(w):
        worker.cancel(spec.schedule_id, owner)
        return original(w)

    worker.token_provider = cancel
    assert worker.run_once()[0]["state"] == "failed"
    assert worker.run_once() == []
    stop = Event()
    statuses = []

    def done(result):
        statuses.append(result)
        stop.set()

    worker.run(stop, on_result=done)
    assert statuses == [[]]


def test_changed_schedule_requires_new_delegation(tmp_path, setup):
    worker, _, _, _, _ = prepare(tmp_path, setup)
    with sqlite3.connect(worker.scheduler.path) as db:
        row = db.execute("SELECT spec FROM business_schedules").fetchone()[0]
        spec = ReportSchedule.model_validate_json(row).model_copy(update={"occurrences": 3})
        db.execute("UPDATE business_schedules SET spec=?", (spec.model_dump_json(),))
    assert worker.run_once()[0]["state"] == "failed"


def test_two_workers_cannot_claim_same_live_job(tmp_path, setup):
    from concurrent.futures import ThreadPoolExecutor

    worker, _, _, _, _ = prepare(tmp_path, setup)
    entered, release = Event(), Event()
    original = worker.token_provider

    def blocking(workload):
        entered.set()
        assert release.wait(5)
        return original(workload)

    worker.token_provider = blocking
    second = ReportWorker(worker.path, scheduler=worker.scheduler, token_provider=original)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(worker.run_once)
        assert entered.wait(5)
        try:
            assert second.run_once() == []
        finally:
            release.set()
        assert first.result()[0]["state"] == "active"
    with worker.scheduler.actions._db() as db:
        assert db.execute("SELECT count(*) FROM business_actions").fetchone()[0] == 1
