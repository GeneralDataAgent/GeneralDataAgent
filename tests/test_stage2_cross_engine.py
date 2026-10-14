import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_cross_engine.py"


def test_stage2_cross_engine_profile_gate_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_cross_engine_evaluation"]
    result = run_evaluation()

    assert result["dataset_size"] == 20
    assert result["passed"] == 20
    assert result["failed"] == 0
    assert result["pass_rate"] == 1.0
    assert result["total_field_checks"] == 380
    assert result["failures"] == []
