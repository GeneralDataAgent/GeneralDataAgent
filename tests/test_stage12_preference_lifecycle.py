import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_stage12_display_preferences import actor, allow, confirmation, store

from generaldataagent.memory.display_preferences import (
    ConfirmedDisplayPreferenceStore,
    PreferenceConflict,
    PreferenceUnavailable,
)
from generaldataagent.memory.preference_lifecycle import (
    PreferenceHistoryMaintenance,
    PreferencePurgePlan,
)
from generaldataagent.services import preference_maintenance as cli


def history(tmp_path, count=3, *, max_versions=1000):
    now = [datetime(2026, 10, 1, tzinfo=UTC)]
    memory = store(tmp_path, clock=lambda: now[0], max_versions=max_versions)
    for version in range(count):
        memory.confirm(actor(), confirmation(version, layout="table", ttl=30 * 86400), guard=allow)
        now[0] += timedelta(hours=1)
    now[0] += timedelta(days=8)
    return memory, now, PreferenceHistoryMaintenance(memory)


def test_dry_run_then_reviewed_row_purge_retains_latest_and_replays_as_absent(tmp_path):
    memory, _, maintenance = history(tmp_path)
    latest = memory.current(actor())
    old = memory.history(actor(), 1)
    plan = maintenance.plan(actor(), guard=allow)
    assert plan.scanned == 2 and plan.candidate_versions == (1, 2)
    assert memory.history(actor(), 1) == old  # Planning never deletes.
    result = maintenance.apply(actor(), plan, guard=allow)
    assert result.deleted == 2 and result.already_absent == 0
    assert memory.current(actor()) == latest
    with pytest.raises(PermissionError):
        memory.history(actor(), 1)
    repeated = maintenance.apply(actor(), plan, guard=allow)
    assert repeated.deleted == 0 and repeated.already_absent == 2
    events = [
        json.loads(line) for line in (tmp_path / "preference-audit.jsonl").read_text().splitlines()
    ]
    intents = [e for e in events if e["event_type"] == "display_preference.purge_intent"]
    assert len(intents) == 2 and intents[0]["task_id"] == str(plan.plan_id)
    assert "value" not in intents[0]["payload"] and "plan_token" not in intents[0]["payload"]


def test_bounded_pages_preserve_revision_high_watermark_and_release_capacity(tmp_path):
    memory, now, maintenance = history(tmp_path, count=4, max_versions=4)
    with pytest.raises(PreferenceConflict, match="capacity"):
        memory.confirm(actor(), confirmation(4), guard=allow)
    first = maintenance.plan(actor(), scan_limit=1, guard=allow)
    assert first.candidate_versions == (1,) and first.has_more and first.next_after_version == 1
    maintenance.apply(actor(), first, guard=allow)
    second = maintenance.plan(actor(), after_version=1, scan_limit=1, guard=allow)
    assert second.candidate_versions == (2,) and second.has_more
    maintenance.apply(actor(), second, guard=allow)
    third = maintenance.plan(actor(), after_version=2, scan_limit=1, guard=allow)
    assert third.candidate_versions == (3,) and not third.has_more
    maintenance.apply(actor(), third, guard=allow)
    assert memory.current(actor()).version == 4
    # Reopen and append above the old head; never reuse deleted revision IDs.
    reopened = store(tmp_path, clock=lambda: now[0], max_versions=4)
    assert reopened.confirm(actor(), confirmation(4), guard=allow).version == 5
    with pytest.raises(PreferenceConflict, match="head_changed"):
        maintenance.apply(actor(), first, guard=allow)


@pytest.mark.parametrize("head_kind", ["active", "expired", "revoked"])
def test_latest_value_or_tombstone_is_always_retained_without_resurrecting_history(
    tmp_path, head_kind
):
    memory, now, maintenance = history(tmp_path, count=1)
    if head_kind == "revoked":
        memory.revoke(actor(), 1, guard=allow)
    if head_kind == "expired":
        now[0] += timedelta(days=31)
    now[0] += timedelta(days=8)
    before = memory.current(actor())
    plan = maintenance.plan(actor(), guard=allow)
    maintenance.apply(actor(), plan, guard=allow)
    assert memory.current(actor()) == before
    assert before.version not in plan.candidate_versions
    assert memory.history(actor(), before.version) is not None
    if head_kind == "revoked":
        assert before.record is None and before.status == "revoked"
        with pytest.raises(PermissionError):
            memory.history(actor(), 1)


def test_retention_respects_supersession_or_expiry_and_never_promotes_older_value(tmp_path):
    memory, now, maintenance = history(tmp_path, count=2)
    now[0] = datetime(2026, 10, 1, 2, tzinfo=UTC)
    fresh = maintenance.plan(actor(), guard=allow)
    assert fresh.scanned == 1 and fresh.candidate_versions == ()
    now[0] += timedelta(days=7)
    matured = maintenance.plan(actor(), guard=allow)
    assert matured.candidate_versions == (1,)
    maintenance.apply(actor(), matured, guard=allow)
    assert memory.current(actor()).version == 2


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
def test_plan_cannot_apply_under_another_owner_or_domain_view(tmp_path, change):
    memory, _, maintenance = history(tmp_path)
    plan = maintenance.plan(actor(), guard=allow)
    with pytest.raises(PermissionError):
        maintenance.apply(actor().model_copy(update=change), plan, guard=allow)
    assert memory.history(actor(), 1) is not None


def test_plan_does_not_transfer_to_cloned_store_at_another_location(tmp_path):
    memory, now, maintenance = history(tmp_path)
    plan = maintenance.plan(actor(), guard=allow)
    copied_path = tmp_path / "copied.db"
    shutil.copyfile(memory.path, copied_path)
    copy = ConfirmedDisplayPreferenceStore(
        copied_path, encryption_key=b"p" * 32, audit=memory.audit, clock=lambda: now[0]
    )
    with pytest.raises(PermissionError):
        PreferenceHistoryMaintenance(copy).apply(actor(), plan, guard=allow)
    assert copy.history(actor(), 1) is not None


@pytest.mark.parametrize(
    "fault", ["metadata", "token", "expired", "clock_backwards", "head_changed", "row_corrupt"]
)
def test_tampered_stale_or_unknown_plans_never_delete_rows(tmp_path, fault):
    memory, now, maintenance = history(tmp_path)
    plan = maintenance.plan(actor(), guard=allow)
    if fault == "metadata":
        plan = plan.model_copy(update={"candidate_versions": (1,)})
    elif fault == "token":
        plan = plan.model_copy(
            update={"plan_token": ("A" if plan.plan_token[0] != "A" else "B") + plan.plan_token[1:]}
        )
    elif fault == "expired":
        now[0] += timedelta(seconds=300)
    elif fault == "clock_backwards":
        now[0] -= timedelta(seconds=1)
    elif fault == "head_changed":
        memory.confirm(actor(), confirmation(3), guard=allow)
    else:
        with sqlite3.connect(memory.path) as db:
            db.execute("UPDATE display_preferences SET body=X'00' WHERE version=1")
    with pytest.raises((PermissionError, PreferenceConflict)):
        maintenance.apply(actor(), plan, guard=allow)
    with sqlite3.connect(memory.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM display_preferences WHERE version IN (1,2)"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("fault", ["audit", "grant", "commit"])
def test_audit_revocation_or_commit_failure_rolls_back_the_entire_batch(
    tmp_path, monkeypatch, fault
):
    memory, _, maintenance = history(tmp_path)
    plan = maintenance.plan(actor(), guard=allow)
    check = allow
    if fault == "audit":
        memory.audit.emit = lambda _: (_ for _ in ()).throw(OSError("audit unavailable"))
    elif fault == "grant":
        calls = [0]

        def check():
            calls[0] += 1
            if calls[0] == len(plan.candidate_versions) + 3:
                raise PermissionError("grant revoked after deletes")
    else:
        connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def __exit__(self, exc_type, exc, traceback):
                if exc_type is None:
                    self.rollback()
                    raise sqlite3.OperationalError("synthetic commit failure")
                return super().__exit__(exc_type, exc, traceback)

        monkeypatch.setattr(
            sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=FailedCommit)
        )
    with pytest.raises((PermissionError, PreferenceUnavailable)):
        maintenance.apply(actor(), plan, guard=check)
    monkeypatch.undo()
    assert memory.history(actor(), 1) is not None
    assert memory.history(actor(), 2) is not None
    assert memory.current(actor()).version == 3


def test_planning_refuses_clock_inversion_and_corrupt_lookahead(tmp_path):
    memory, now, maintenance = history(tmp_path, count=1)
    now[0] = datetime(2026, 9, 1, tzinfo=UTC)
    memory.confirm(actor(), confirmation(1), guard=allow)
    now[0] = datetime(2026, 11, 1, tzinfo=UTC)
    with pytest.raises(PreferenceUnavailable, match="clock_order"):
        maintenance.plan(actor(), guard=allow)
    with sqlite3.connect(memory.path) as db:
        db.execute("UPDATE display_preferences SET body=X'00' WHERE version=1")
    with pytest.raises(PermissionError):
        maintenance.plan(actor(), guard=allow)


@pytest.mark.parametrize(
    "limits",
    [
        {"scan_limit": 0},
        {"scan_limit": 201},
        {"scan_limit": True},
        {"after_version": -1},
        {"after_version": True},
        {"retention_seconds": 59},
        {"retention_seconds": 90 * 86400 + 1},
    ],
)
def test_limits_fail_before_planning_or_deletion(tmp_path, limits):
    _, _, maintenance = history(tmp_path)
    with pytest.raises(ValueError):
        maintenance.plan(actor(), guard=allow, **limits)


def test_empty_history_produces_noop_and_bad_cursor_is_rejected(tmp_path):
    memory = store(tmp_path)
    maintenance = PreferenceHistoryMaintenance(memory)
    plan = maintenance.plan(actor(), guard=allow)
    assert plan.scanned == 0 and plan.head_version == 0 and not plan.has_more
    assert maintenance.apply(actor(), plan, guard=allow).deleted == 0
    with pytest.raises(PreferenceConflict):
        maintenance.plan(actor(), after_version=1, guard=allow)


def test_cli_defaults_to_plan_and_requires_explicit_apply_file_and_live_grant(
    tmp_path, monkeypatch, capsys
):
    from test_metric_analysis import build_runtime
    from test_stage11_task_submission import assembly, credentials

    from generaldataagent.orchestration.task_presentation import MetricDisplayPreferenceService

    memory, _, _ = history(tmp_path)
    with assembly(tmp_path, build_runtime(tmp_path)) as (tasks, _):
        token, _ = credentials(tasks.broker)
        service = MetricDisplayPreferenceService(tasks=tasks, store=memory)
        monkeypatch.setattr(cli, "load_service", lambda _: service)
        monkeypatch.setenv("GDA_PREFERENCE_MAINTENANCE_TOKEN", token)
        args = ["--factory", "trusted:factory", "--data-domain", "commerce"]
        assert cli.main(args) == 2
        assert "explicit_local" in capsys.readouterr().out
        args += ["--development"]
        assert cli.main(args) == 0
        plan_text = capsys.readouterr().out
        plan = PreferencePurgePlan.model_validate_json(plan_text)
        assert plan.candidate_versions == (1, 2)
        assert token not in plan_text and "Bearer" not in plan_text
        assert memory.history(actor(), 1) is not None
        path = tmp_path / "reviewed-plan.json"
        path.write_text(plan_text)
        assert cli.main(args + ["--apply-plan", str(path), "--scan-limit", "1"]) == 2
        capsys.readouterr()
        assert cli.main(args + ["--apply-plan", str(path)]) == 0
        assert json.loads(capsys.readouterr().out)["deleted"] == 2
        tasks.broker.revoke(token)
        assert cli.main(args) == 2
        output = capsys.readouterr().out
        assert token not in output and "unavailable" in output


def test_real_subprocess_cli_and_presentation_consumer():
    import subprocess
    import sys
    from pathlib import Path

    process = subprocess.run(
        [sys.executable, "-m", "examples.preference_maintenance_demo"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "actual_subprocess_cli": True,
        "dry_run_candidates": [1],
        "deleted": 1,
        "replay_already_absent": 1,
        "head_retained": 2,
        "next_version": 3,
        "same_presentation_after_purge": True,
        "history_unavailable": True,
        "model_calls": 0,
        "production_data_purged": False,
    }


def test_concurrent_apply_of_same_plan_does_not_repeat_deletes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    memory, _, maintenance = history(tmp_path)
    plan = maintenance.plan(actor(), guard=allow)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: maintenance.apply(actor(), plan, guard=allow), range(2)))
    assert sorted((r.deleted, r.already_absent) for r in results) == [(0, 2), (2, 0)]
    assert memory.current(actor()).version == 3


def test_corrupt_lookahead_and_expiry_during_audit_leave_all_rows(tmp_path):
    memory, now, maintenance = history(tmp_path)
    plan = maintenance.plan(actor(), guard=allow)
    emit = memory.audit.emit

    def slow_audit(event):
        emit(event)
        now[0] += timedelta(seconds=300)

    memory.audit.emit = slow_audit
    with pytest.raises(PermissionError):
        maintenance.apply(actor(), plan, guard=allow)
    assert memory.history(actor(), 1) is not None
    with sqlite3.connect(memory.path) as db:
        db.execute("UPDATE display_preferences SET body=X'00' WHERE version=2")
    with pytest.raises(PermissionError):
        maintenance.plan(actor(), scan_limit=1, guard=allow)
    assert memory.history(actor(), 1) is not None


@pytest.mark.parametrize("fault", ["permission", "domain", "oversize", "invalid_json"])
def test_cli_refuses_invalid_scope_or_plan_without_deleting(tmp_path, monkeypatch, capsys, fault):
    from test_metric_analysis import build_runtime
    from test_stage11_task_submission import assembly, credentials

    from generaldataagent.orchestration.task_presentation import MetricDisplayPreferenceService

    memory, _, _ = history(tmp_path)
    with assembly(tmp_path, build_runtime(tmp_path)) as (tasks, _):
        permissions = frozenset() if fault == "permission" else frozenset({"analysis:run"})
        token, _ = credentials(tasks.broker, permissions=permissions)
        monkeypatch.setattr(
            cli, "load_service", lambda _: MetricDisplayPreferenceService(tasks=tasks, store=memory)
        )
        monkeypatch.setenv("GDA_PREFERENCE_MAINTENANCE_TOKEN", token)
        domain = "finance" if fault == "domain" else "commerce"
        args = ["--factory", "trusted:factory", "--development", "--data-domain", domain]
        if fault in {"oversize", "invalid_json"}:
            path = tmp_path / "untrusted-plan.json"
            path.write_text("x" * 131073 if fault == "oversize" else "{}")
            args += ["--apply-plan", str(path)]
        assert cli.main(args) == 2
        output = capsys.readouterr().out
        assert output.strip() == '{"status": "preference_maintenance_unavailable"}'
        assert token not in output
        assert memory.history(actor(), 1) is not None


def test_cli_factory_is_operator_owned_and_requires_durable_service(monkeypatch):
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="trusted_module"):
        cli.load_service("module:factory();unsafe")
    monkeypatch.setattr(
        cli.importlib, "import_module", lambda _: SimpleNamespace(factory=lambda: object())
    )
    with pytest.raises(ValueError, match="durable_local"):
        cli.load_service("module:factory")
