import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("disconnect", [False, True])
def test_real_http_stream_receives_status_before_worker_and_drains_live_progress(disconnect):
    result = subprocess.run(
        [sys.executable, "-m", "examples.task_stream_demo"]
        + (["--disconnect"] if disconnect else []),
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=40,
        env=os.environ | {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["first_status"] == "queued"
    assert report["worker_status"] == "completed"
    if not disconnect:
        assert report["progress_sequences"] == list(range(1, 8))
        assert report["end"] == {"schema_version": "1.0", "reason": "terminal", "after": 7}
    assert report["disconnected_early"] == disconnect
    assert report["active_connections"] == 0
    assert report["real_http"] is True and report["model_calls"] == 0


def test_real_http_cancel_reports_pending_then_confirmed_stop():
    result = subprocess.run(
        [sys.executable, "-m", "examples.task_cancel_demo"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=40,
        env=os.environ | {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "real_http": True,
        "statuses": ["cancel_requested", "cancelled"],
        "end": "terminal",
        "worker_status": "cancelled",
        "model_calls": 0,
    }
