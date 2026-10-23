"""Observed rule-team stops and usage, without storing model prose or prompts."""

from collections import Counter

from langchain_core.callbacks import BaseCallbackHandler

from generaldataagent.agents.rule_supervisor import RULE_ROLE_SEQUENCE

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


class RuleCallDiagnostics(BaseCallbackHandler):
    """Model declarations are diagnostic observations, never authorization evidence."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.events.append({"status": "started"})

    def on_llm_end(self, response, **kwargs) -> None:
        for batch in response.generations:
            for generation in batch:
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                calls = []
                for call in getattr(message, "tool_calls", []):
                    arguments = call["args"]
                    entry = {
                        "name": call["name"],
                        "argument_keys": sorted(arguments),
                        "subagent_type": arguments.get("subagent_type")
                        if arguments.get("subagent_type") in RULE_ROLE_SEQUENCE
                        else None,
                    }
                    # Keep only finite enums. In particular, a model's summary,
                    # limitations and task ids never enter this diagnostic log.
                    if call["name"] == "SpecialistResult":
                        entry["reported_role"] = (
                            arguments.get("role")
                            if arguments.get("role") in RULE_ROLE_SEQUENCE
                            else None
                        )
                        entry["reported_status"] = (
                            arguments.get("status")
                            if arguments.get("status") in {"completed", "failed", "needs_human"}
                            else None
                        )
                    calls.append(entry)
                self.events.append(
                    {
                        "status": "response",
                        "tool_calls": calls,
                        "usage": getattr(message, "usage_metadata", None),
                        "finish_reason": message.response_metadata.get("finish_reason"),
                    }
                )

    def on_llm_error(self, error, **kwargs) -> None:
        status = getattr(error, "status_code", None)
        self.events.append(
            {
                "status": "error",
                "error": type(error).__name__,
                "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            }
        )

    def on_tool_error(self, error, **kwargs) -> None:
        self.events.append({"status": "tool_error", "error": type(error).__name__})

    def usage(self) -> dict:
        totals = dict.fromkeys(_TOKEN_KEYS, 0)
        responses = [event for event in self.events if event["status"] == "response"]
        complete = bool(responses)
        for event in responses:
            usage = event.get("usage")
            if not isinstance(usage, dict):
                complete = False
                continue
            for key in totals:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    totals[key] += value
                else:
                    complete = False
        attempts = sum(event["status"] == "started" for event in self.events)
        if attempts != len(responses) or any(event["status"] == "error" for event in self.events):
            complete = False
        return {**totals, "telemetry_complete": complete, "known_tokens_only": not complete}

    def snapshot(self) -> dict:
        calls = [call for event in self.events for call in event.get("tool_calls", [])]
        return {
            "model_attempts": sum(event["status"] == "started" for event in self.events),
            "model_calls": sum(event["status"] == "response" for event in self.events),
            "model_errors": [event for event in self.events if event["status"] == "error"],
            "tool_errors": [event for event in self.events if event["status"] == "tool_error"],
            "usage": self.usage(),
            "tool_calls": calls,
            "specialist_reports": [
                {"role": call["reported_role"], "status": call["reported_status"]}
                for call in calls
                if call["name"] == "SpecialistResult"
            ],
            "finish_reasons": [
                event["finish_reason"] for event in self.events if event["status"] == "response"
            ],
            "delegations": [call["subagent_type"] for call in calls if call["name"] == "task"],
        }


def stop_diagnostic(row: dict) -> dict:
    """Describe the evidence we have, without inferring a reason from an absent role."""
    roles = row.get("delegated_roles") or []
    reports = row.get("specialist_reports") or []
    if row["outcome"] == "refused_by_parser":
        code = "parser_refusal"
    elif row["outcome"] == "service_circuit_open":
        code = "service_circuit_open"
    elif row.get("passed"):
        code = "completed"
    elif row.get("model_errors"):
        code = "model_error_observed"
    elif row.get("tool_errors"):
        code = "tool_error_observed"
    elif row["outcome"] == "supervisor_error":
        code = "supervisor_exception"
    elif any(report["status"] in {"failed", "needs_human"} for report in reports):
        code = "specialist_stop_reported"
    elif row.get("status") == "completed":
        code = "delivery_check_failed"
    else:
        code = "incomplete_sequence_reason_unknown"
    return {
        "code": code,
        "last_observed_role": roles[-1] if roles else None,
        "reported_stops": [
            report for report in reports if report["status"] in {"failed", "needs_human"}
        ],
        "semantic_root_cause": "not_established" if code != "completed" else None,
    }


def summarize_rule_run(results: list[dict], *, planned_n: int) -> dict:
    """Keep planned denominator and known usage from successful AND exceptional rows."""
    model_runs = [row for row in results if row["outcome"] == "model_run"]
    attempted = [
        row
        for row in results
        if row["outcome"] not in {"refused_by_parser", "service_circuit_open"}
    ]
    return {
        "n": planned_n,
        "processed_n": len(results),
        "pending_n": planned_n - len(results),
        "refused_by_parser_n": sum(row["outcome"] == "refused_by_parser" for row in results),
        "model_run_n": len(model_runs),
        "attempted_n": len(attempted),
        "service_circuit_open_n": sum(row["outcome"] == "service_circuit_open" for row in results),
        "supervisor_error_n": sum(row["outcome"] == "supervisor_error" for row in results),
        "model_completed_n": sum(row.get("status") == "completed" for row in model_runs),
        "roles_complete_n": sum(row.get("roles_complete", False) for row in model_runs),
        "answer_match_n": sum(row.get("answer_matches_published", False) for row in model_runs),
        "passed_over_all_questions": sum(row["passed"] for row in results),
        "model_calls": sum(row.get("model_calls", 0) for row in results),
        "model_attempts": sum(row.get("model_attempts", 0) for row in results),
        "usage": {
            key: sum(row.get("usage", {}).get(key, 0) for row in results) for key in _TOKEN_KEYS
        },
        "usage_telemetry_complete": (
            all(row.get("usage", {}).get("telemetry_complete") is True for row in attempted)
            if attempted
            else None
        ),
        "stop_diagnostics": dict(Counter(stop_diagnostic(row)["code"] for row in results)),
        "production_accepted": False,
    }
