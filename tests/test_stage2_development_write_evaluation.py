import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_development_write.py"


def test_stage2_development_write_gate_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_development_write_evaluation"]

    result = run_evaluation()

    assert result["check_count"] == 22
    assert result["passed"] == 22
    assert result["failed"] == 0
