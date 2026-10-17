import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_stage4_cli_distinguishes_local_security_from_missing_production_evidence():
    for strict, expected in ((False, 0), (True, 1)):
        args = [sys.executable, "evals/run_stage4_acceptance.py"]
        if strict:
            args.append("--require-production")
        result = subprocess.run(
            args,
            cwd=ROOT,
            env={**os.environ, "LANGSMITH_TRACING": "false"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stderr
        report = json.loads(result.stdout)
        assert report["development_gate_status"] == "passed"
        assert report["security_regression"]["passed"] == 91
        assert report["stage_acceptance_status"] == "incomplete"
        assert "missing_independent_release_evidence" in report["blockers"]
