import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_schema.py"


def test_stage2_schema_diff_matrix_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_schema_evaluation"]
    result = run_evaluation()

    assert result["dataset_size"] == 20
    assert result["passed"] == 20
    assert result["failed"] == 0
    assert result["pass_rate"] == 1.0
