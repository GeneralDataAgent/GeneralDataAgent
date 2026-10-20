"""Bounded task planning: public text is authoritative; model interpretations are proposals."""

import json
import re

from generaldataagent.contracts.analysis import AnalysisTaskSpec, ToolAction


class ActionSchemaError(ValueError):
    def __init__(self, paths):
        self.paths = paths
        super().__init__("invalid typed action")


def validate_action(action: dict) -> dict:
    """Validate without persisting payload values in schema-error telemetry."""
    from pydantic import ValidationError

    try:
        parsed = ToolAction.model_validate_json(json.dumps(action))
    except ValidationError as exc:
        raise ActionSchemaError(
            [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        ) from None
    return parsed.model_dump(mode="python", exclude_unset=True, exclude_none=True)


def accept_contract(proposal: dict, *, question: str, instructions: str) -> AnalysisTaskSpec:
    spec = AnalysisTaskSpec.model_validate(proposal)
    if spec.question != question or spec.public_requirements != instructions:
        raise ValueError("contract must preserve both public task texts exactly")
    if spec.conflicts:
        raise ValueError("public requirement conflicts require clarification")
    # Only explicit rounding phrases count; never infer precision from labels or examples.
    words = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    matches = re.findall(
        r"\b(?:round(?:ed)?(?:\s+the\s+\w+)?|round\s+the\s+result)\s+to\s+"
        r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+decimal\s+places?\b",
        question + "\n" + instructions,
        re.I,
    )
    digits = {int(v) if v.isdigit() else words[v.lower()] for v in matches}
    if len(digits) > 1:
        raise ValueError("conflicting public rounding requirements")
    if digits:
        precision = next(iter(digits))
        if spec.answer.decimals is not None and spec.answer.decimals != precision:
            raise ValueError("answer precision contradicts public rounding requirement")
        spec = AnalysisTaskSpec.model_validate(
            spec.model_dump() | {"answer": spec.answer.model_dump() | {"decimals": precision}}
        )
    return spec


def normalize_protocol_action(action):
    """Normalize unambiguous function envelopes only; never choose between two actions."""
    names = {
        "plan_analysis": "contract",
        "execute_sql": "sql",
        "submit_sql": "final_sql",
        "crosscheck_sql": "crosscheck",
    }
    if not isinstance(action, dict):
        return action
    if set(action) == {"name", "arguments"} and action["name"] in names:
        action = {action["name"]: action["arguments"]}
    if len(action) != 1:
        return action
    name = next(iter(action))
    if name not in names:
        return action
    arguments = action[name]
    if name in {"execute_sql", "submit_sql"}:
        if not isinstance(arguments, dict) or set(arguments) != {"sql"}:
            return action
        arguments = arguments["sql"]
    return {names[name]: arguments}
