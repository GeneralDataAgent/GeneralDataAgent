import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_quality.py"


def test_stage2_quality_rule_matrix_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_quality_evaluation"]
    result = run_evaluation()

    assert result["dataset_size"] == 20
    assert result["rule_type_counts"] == {
        "accepted_values": 5,
        "not_null": 4,
        "range": 7,
        "unique": 4,
    }
    assert result["passed"] == 20
    assert result["failed"] == 0
    assert result["pass_rate"] == 1.0
