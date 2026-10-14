import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_lineage.py"


def test_stage2_lineage_matrix_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_lineage_evaluation"]

    result = run_evaluation()

    assert result["dataset_size"] == 20
    assert result["passed"] == 20
    assert result["failed"] == 0
    assert result["complete_cases"] == 9
    assert result["incomplete_cases"] == 1
    assert result["rejected_cases"] == 10
    assert result["stability_checks"] == 10
