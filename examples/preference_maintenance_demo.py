"""Synthetic subprocess dry-run/apply/replay, citation removal and stable presentation."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.task_submission_demo import _WORKER_STACK, demo_service
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.memory.display_preferences import (
    ConfirmDisplayPreference,
    ConfirmedDisplayPreferenceStore,
    MetricDisplayPreference,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.task_presentation import MetricDisplayPreferenceService


def preference_service(tasks, root, **kwargs):
    memory = ConfirmedDisplayPreferenceStore(
        root / "preferences.db",
        encryption_key=b"p" * 32,
        audit=JsonlAuditSink(root / "preference-audit.jsonl"),
        max_versions=2,
        **kwargs,
    )
    return MetricDisplayPreferenceService(tasks=tasks, store=memory)


def maintenance_factory():
    root = Path(os.environ["GDA_PREFERENCE_MAINTENANCE_DEMO_ROOT"])
    tasks = _WORKER_STACK.enter_context(demo_service(root))
    return preference_service(tasks, root)


def demonstrate(root):
    owner = ActorContext(
        user_id="demo", tenant_id="demo", project_id="demo", data_domains=("commerce",)
    )
    with demo_service(root) as tasks:
        token = tasks.broker.issue(
            user_id="demo",
            tenant_id="demo",
            project_id="demo",
            data_domains=frozenset({"commerce"}),
            permissions=frozenset({"analysis:run"}),
            ttl_seconds=900,
        )
        now = [datetime.now(UTC) - timedelta(days=15)]
        preferences = preference_service(tasks, root, clock=lambda: now[0])
        for version in range(2):
            preferences.confirm(
                owner,
                token,
                ConfirmDisplayPreference(
                    expected_version=version,
                    confirmed=True,
                    value=MetricDisplayPreference(layout="table", preview_rows=1),
                    valid_for_seconds=30 * 86400,
                ),
            )
            now[0] += timedelta(days=5)
        preferences.store.clock = lambda: datetime.now(UTC)
        task = tasks.submit(
            MetricQuery(metric_name="order_count", metric_version="v1"), owner, token, "maintenance"
        )
        assert tasks.run_once().status == "completed"
        before = preferences.presentation(task.task_id, owner, token)
        args = [
            sys.executable,
            "-m",
            "generaldataagent.services.preference_maintenance",
            "--development",
            "--factory",
            "examples.preference_maintenance_demo:maintenance_factory",
            "--data-domain",
            "commerce",
        ]
        env = os.environ | {
            "GDA_PREFERENCE_MAINTENANCE_DEMO_ROOT": str(root),
            "GDA_PREFERENCE_MAINTENANCE_TOKEN": token,
        }

        def command(extra=()):
            process = subprocess.run(
                args + list(extra),
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert process.returncode == 0, "synthetic maintenance command failed"
            assert token not in process.stdout and token not in process.stderr
            return json.loads(process.stdout)

        plan = command()
        assert plan["candidate_versions"] == [1] and plan["head_version"] == 2
        assert preferences.history(owner, token, 1).version == 1
        plan_file = root / "reviewed-plan.json"
        plan_file.write_text(json.dumps(plan))
        result = command(["--apply-plan", str(plan_file)])
        replay = command(["--apply-plan", str(plan_file)])
        assert result["deleted"] == 1 and replay["already_absent"] == 1 and replay["deleted"] == 0
        assert preferences.presentation(task.task_id, owner, token) == before
        try:
            preferences.history(owner, token, 1)
        except PermissionError:
            pass
        else:
            raise AssertionError("purged historical citation remained readable")
        next_state = preferences.confirm(
            owner,
            token,
            ConfirmDisplayPreference(
                expected_version=2,
                confirmed=True,
                value=MetricDisplayPreference(layout="summary"),
            ),
        )
        assert next_state.version == 3
        return {
            "actual_subprocess_cli": True,
            "dry_run_candidates": plan["candidate_versions"],
            "deleted": result["deleted"],
            "replay_already_absent": replay["already_absent"],
            "head_retained": 2,
            "next_version": next_state.version,
            "same_presentation_after_purge": True,
            "history_unavailable": True,
            "model_calls": 0,
            "production_data_purged": False,
        }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-preference-maintenance-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
