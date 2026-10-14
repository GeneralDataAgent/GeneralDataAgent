import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_transformation.py"


def test_stage2_transformation_safety_matrix_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_transformation_evaluation"]
    result = run_evaluation()

    assert result["dataset_size"] == 20
    assert result["accepted_cases"] == 8
    assert result["rejected_cases"] == 12
    assert result["passed"] == 20
    assert result["failed"] == 0
    assert result["pass_rate"] == 1.0
