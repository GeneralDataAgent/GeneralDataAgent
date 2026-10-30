"""Evaluation-only native/JSON action transport and typed wire validation."""

import json

from generaldataagent.contracts.analysis import ToolAction


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


PROTOCOL_ACTION_KEYS = {
    "submit_mixed_sql_answers": "mixed_sql",
    "plan_analysis": "contract",
    "execute_sql": "sql",
    "submit_sql": "final_sql",
    "crosscheck_sql": "crosscheck",
    "compute_sql": "sql",
    "analyze_fee_rules": "rules",
    "fixed_analysis": "analysis",
    "compute_statistics": "analysis",
    "evaluate_regression": "analysis",
    "search_documents": "search",
    "collect_values": "collect",
    "submit_answers": "answers",
    "declare_unsupported": "unsupported",
    "submit_sql_answers": "answer_sql",
    "submit_query_plan": "query_plan",
}


def normalize_protocol_action(action):
    """Normalize unambiguous function envelopes only; never choose between two actions."""
    names = PROTOCOL_ACTION_KEYS
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
    if name == "declare_unsupported":
        return (
            {"unsupported": arguments["reason"]}
            if isinstance(arguments, dict) and set(arguments) == {"reason"}
            else action
        )
    if name == "search_documents":
        return (
            {"search": arguments["query"]}
            if isinstance(arguments, dict) and set(arguments) == {"query"}
            else action
        )
    if name in {"collect_values", "compute_sql", "submit_sql_answers"}:
        allowed = {"sql", "collection_kind"} if name == "collect_values" else {"sql"}
        if not isinstance(arguments, dict) or "sql" not in arguments or set(arguments) - allowed:
            return action
        return {names[name]: arguments if name == "collect_values" else arguments["sql"]}
    if name in {"compute_statistics", "evaluate_regression"}:
        if not isinstance(arguments, dict) or "table" not in arguments:
            return action
        request = {key: value for key, value in arguments.items() if key != "table"}
        if name == "compute_statistics":
            return {
                "analysis": {
                    "table": arguments["table"],
                    "columns": request.get("columns", []),
                    "operation": "statistics",
                    "statistics": request,
                }
            }
        features = request.get("features", [])
        return {
            "analysis": {
                "table": arguments["table"],
                "columns": (features if isinstance(features, list) else [])
                + [request.get("target", "")],
                "operation": "regression_evaluation",
                "experiment": request,
            }
        }
    if name == "fixed_analysis":
        return {"analysis": arguments}
    if name == "submit_mixed_sql_answers":
        return {"mixed_sql": arguments}
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
