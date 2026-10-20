"""Bounded task planning: public text is authoritative; model interpretations are proposals."""

from generaldataagent.contracts.analysis import AnalysisTaskSpec, ToolAction


class ActionSchemaError(ValueError):
    def __init__(self, paths):
        self.paths = paths
        super().__init__("invalid typed action")


def validate_action(action: dict) -> dict:
    """Validate without persisting payload values in schema-error telemetry."""
    from pydantic import ValidationError

    try:
        parsed = ToolAction.model_validate(action)
    except ValidationError as exc:
        raise ActionSchemaError(
            [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        ) from None
    return parsed.model_dump(mode="python", exclude_unset=True)


def accept_contract(proposal: dict, *, question: str, instructions: str) -> AnalysisTaskSpec:
    spec = AnalysisTaskSpec.model_validate(proposal)
    if spec.question != question or spec.public_requirements != instructions:
        raise ValueError("contract must preserve both public task texts exactly")
    if spec.conflicts:
        raise ValueError("public requirement conflicts require clarification")
    return spec
