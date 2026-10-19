"""Assemble separately verified action, optimization and Stage 4 release gates."""

from generaldataagent.evaluation.stage_five import KINDS


def assemble_stage_five_report(
    *,
    revision: str,
    local: dict,
    external: list[dict],
    optimization: dict,
    release: dict,
    dirty: bool,
) -> dict:
    blockers = []
    gates = {g["kind"]: g for g in external}
    if len(external) != len(KINDS) or set(gates) != set(KINDS):
        blockers.append("invalid_external_action_gate_set")
    for kind in KINDS:
        gate = gates.get(kind, {})
        if gate.get("status") != "passed" or gate.get("blockers"):
            blockers.append(f"missing_or_failed_external_{kind}_qualification")
    if optimization.get("status") != "passed" or optimization.get("blockers"):
        blockers.append("missing_production_cost_latency_comparison")
    if release.get("status") != "passed" or release.get("blockers"):
        blockers.append("missing_independent_stage4_release_admission")
    if dirty:
        blockers.append("uncommitted_source_changes")
    local_passed = (
        local.get("total") == 42 and local.get("passed") == 42 and local.get("failed") == 0
    )
    if not local_passed:
        blockers.append("local_business_regression_failed")
    return {
        "stage": 5,
        "revision": revision,
        "development_gate_status": "passed" if local_passed else "failed",
        "business_regression": local,
        "external_action_gates": external,
        "optimization_gate": optimization,
        "stage4_admission_gate": release,
        "stage_acceptance_status": "incomplete" if blockers else "passed",
        "blockers": blockers,
    }
