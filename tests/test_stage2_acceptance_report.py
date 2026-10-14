import runpy
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_stage2_acceptance.py"


def test_stage2_acceptance_report_separates_local_and_external_gates() -> None:
    run_report = runpy.run_path(RUNNER)["run_acceptance_report"]
    report = run_report()

    assert report["stage"] == 2
    assert report["development_gate_status"] == "passed"
    assert report["stage_acceptance_status"] == "incomplete"
    assert report["stage_acceptance_complete"] is False
    assert {gate["gate_id"] for gate in report["development_gates"]} == {
        "approval_exactly_once",
        "cross_engine_profile_parity",
        "frozen_components",
        "impact_analysis_matrix",
        "local_parallel_load",
        "quality_rule_matrix",
        "schema_drift_fail_closed",
    }
    assert all(gate["status"] == "passed" for gate in report["development_gates"])
    assert {gate["gate_id"] for gate in report["external_gates"]} == {
        "authoritative_catalog",
        "authoritative_profiler",
        "external_approval_service",
        "production_scale_load",
        "real_dbt_project",
    }
    assert all(gate["status"] == "not_run" for gate in report["external_gates"])
