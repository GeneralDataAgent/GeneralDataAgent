import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_isolation.py"


def test_stage2_fifty_group_isolation_gate_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_isolation_evaluation"]

    result = run_evaluation()

    assert result["passed"] is True
    assert result["group_count"] == 50
    assert result["request_count"] == 100
    assert result["failed_groups"] == 0
    assert result["artifact_count"] == 100
    assert result["unique_task_ids"] == 100
