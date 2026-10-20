"""Bounded task planning: public text is authoritative; model interpretations are proposals."""

import json

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
    from generaldataagent.tools.semantic_requirements import validate_requirement_sources

    validate_requirement_sources(spec.requirements, question, instructions)
    if spec.conflicts:
        raise ValueError("public requirement conflicts require clarification")
    from generaldataagent.tools.answer_fields import public_precisions, validate_field_sources

    validate_field_sources(spec.answer.fields, spec.answer.columns, question, instructions)
    digits = public_precisions(question + "\n" + instructions)
    if len(digits) > 1:
        if (
            not spec.answer.fields
            or spec.answer.decimals is not None
            or any(field.decimals is None for field in spec.answer.fields.values())
        ):
            raise ValueError(
                "conflicting public rounding requirements need complete per-field rules"
            )
        return spec
    if not digits and spec.answer.decimals is not None:
        raise ValueError("precision requires an explicit public rounding rule")
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
        "compute_sql": "sql",
        "fixed_analysis": "analysis",
        "search_documents": "search",
        "collect_values": "collect",
        "submit_answers": "answers",
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
    if name == "submit_answers" and isinstance(arguments, dict) and set(arguments) == {"fields"}:
        fields = arguments["fields"]
        if not isinstance(fields, list) or not fields:
            return action
        bindings, rules = {}, {}
        for field in fields:
            if (
                not isinstance(field, dict)
                or set(field) - {"name", "step", "path", "rule"}
                or not {"name", "step", "path"} <= set(field)
                or not isinstance(field["name"], str)
                or field["name"] in bindings
            ):
                return action
            bindings[field["name"]] = [field["step"], field["path"]]
            if "rule" in field:
                rules[field["name"]] = field["rule"]
        if any(type(v[0]) is not int for v in bindings.values()):
            return action
        return {
            "answers": {},
            "bindings": bindings,
            "evidence_steps": sorted({v[0] for v in bindings.values()}),
            "field_rules": rules,
        }
    if name == "search_documents":
        return (
            {"search": arguments["query"]}
            if isinstance(arguments, dict) and set(arguments) == {"query"}
            else action
        )
    if name in {"collect_values", "compute_sql"}:
        if not isinstance(arguments, dict) or set(arguments) != {"sql"}:
            return action
        return {names[name]: arguments if name == "collect_values" else arguments["sql"]}
    if name == "fixed_analysis":
        return {"analysis": arguments}
    if name == "submit_sql" and isinstance(arguments, dict) and "output_columns" in arguments:
        if set(arguments) - {"sql", "output_columns", "shape", "ties", "requirements"}:
            return action
        if "sql" not in arguments:
            return action
        answer = {"columns": arguments["output_columns"]}
        for field in ("shape", "ties"):
            if field in arguments:
                answer[field] = arguments[field]
        spec = {"answer": answer}
        if "requirements" in arguments:
            spec["requirements"] = arguments["requirements"]
        return {"final_sql": arguments["sql"], "task_spec": spec}
    if name in {"execute_sql", "submit_sql"}:
        if (
            not isinstance(arguments, dict)
            or set(arguments) - {"sql", "task_spec"}
            or "sql" not in arguments
        ):
            return action
        result = {names[name]: arguments["sql"]}
        if "task_spec" in arguments:
            result["task_spec"] = arguments["task_spec"]
        return result
    return {names[name]: arguments}
