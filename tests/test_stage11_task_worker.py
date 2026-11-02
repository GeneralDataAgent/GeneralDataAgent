import json
import subprocess
import sys
from pathlib import Path

from generaldataagent.services.metric_task_worker import main


def test_actual_subprocess_acceptance_restart_worker_and_replay():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "examples/task_submission_demo.py")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["accepted_status"] == "queued"
    assert report["status"] == "completed"
    assert report["task_id"] != report["thread_id"]
    assert report["worker"] == {"status": "completed"}
    assert report["attempts"] == 1 and report["events"] == 7
    assert report["model_calls"] == 0


def test_worker_requires_explicit_local_mode_and_hides_configuration_exception(capsys):
    assert main(["--factory", "unknown:service", "--once"]) == 2
    assert "local_development_configuration_required" in capsys.readouterr().out
    assert main(["--factory", "unknown:service", "--development", "--once"]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "worker_configuration_unavailable"}
    assert main(["--factory", "unknown:service", "--development", "--poll-seconds", "nan"]) == 2
