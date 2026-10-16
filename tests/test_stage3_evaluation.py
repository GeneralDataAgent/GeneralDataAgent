from pathlib import Path

from generaldataagent.evaluation.stage_three import (
    load_stage_three_cases,
    run_stage_three_evaluation,
)

DATASET = Path(__file__).parents[1] / "evals" / "datasets" / "stage3.jsonl"


class _RecordingSandbox:
    runtime_fingerprint = "sha256:" + "a" * 64

    def __init__(self) -> None:
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        from generaldataagent.data_science.sandbox import FixedTemplateSandbox

        return FixedTemplateSandbox().run(**kwargs)


def test_stage_three_dataset_is_frozen_and_balanced() -> None:
    cases = load_stage_three_cases(DATASET)
    assert len(cases) == 20
    assert sum(case.expected == "passed" for case in cases) == 8
    assert sum(case.expected == "rejected" for case in cases) == 12
    assert {case.problem_type for case in cases} == {"binary_classification", "regression"}
    assert {case.split_strategy for case in cases} == {"random", "temporal", "group"}


def test_stage_three_frozen_gate_passes(tmp_path: Path) -> None:
    report = run_stage_three_evaluation(DATASET, tmp_path / "artifacts")
    assert report.total == 20
    assert report.passed == 20
    assert report.failed == 0
    assert report.success_rate == 1.0


def test_stage_three_evaluation_can_use_an_injected_sandbox(tmp_path: Path) -> None:
    sandbox = _RecordingSandbox()
    report = run_stage_three_evaluation(DATASET, tmp_path / "artifacts", sandbox=sandbox)

    assert report.failed == 0
    assert sandbox.calls == 9
