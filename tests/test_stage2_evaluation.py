import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2.py"


def test_frozen_stage2_evaluation_baseline_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_evaluation"]
    result = run_evaluation()

    assert result["dataset_size"] == 25
    assert result["category_counts"] == {
        "impact": 5,
        "profile_oracle": 5,
        "quality": 5,
        "schema_diff": 5,
        "transformation": 5,
    }
    assert result["passed"] == 25
    assert result["failed"] == 0
    assert result["pass_rate"] == 1.0
