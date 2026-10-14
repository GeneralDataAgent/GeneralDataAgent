import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_dbt_adapter.py"


def test_stage2_local_dbt_adapter_matrix_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_dbt_adapter_evaluation"]
    result = run_evaluation()

    assert result["check_count"] == 16
    assert result["passed"] == 16
    assert result["failed"] == 0
