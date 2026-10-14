import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_quality_proposals.py"


def test_stage2_quality_proposal_gate_passes() -> None:
    run_evaluation = runpy.run_path(RUNNER)["run_quality_proposal_evaluation"]

    result = run_evaluation()

    assert result["check_count"] == 12
    assert result["passed"] == 12
    assert result["failed"] == 0
