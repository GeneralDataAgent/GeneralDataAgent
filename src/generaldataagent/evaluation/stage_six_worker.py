"""One answer-blind evaluation worker. Only repository-owned code is executed."""

import json
import sys
from pathlib import Path

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.evaluation.stage_six_models import (
    build_evaluation_model,
    resolve_evaluation_model,
)


def main():
    payload = json.loads(sys.stdin.read())
    provider, route = resolve_evaluation_model(
        app_type=payload["app_type"],
        provider_id=payload["provider_id"],
        model_override=payload.get("model_override"),
        transport=payload.get("transport", "configured"),
        thinking_mode=payload.get("thinking_mode", "default"),
        json_mode=payload.get("json_mode", False),
    )
    if payload.get("route_hash") and payload["route_hash"] != route["route_hash"]:
        raise ValueError("evaluation route changed after snapshot")
    model = build_evaluation_model(provider, route)
    if payload.get("request_interval_seconds", 0) > 0:
        from generaldataagent.evaluation.stage_six_control import PacedModel, SharedRequestPacer

        scope = route["provider_id"] + ":" + route["endpoint_hash"]
        model = PacedModel(
            model,
            SharedRequestPacer(
                Path(payload["pacing_database"]),
                scope=scope,
                interval_seconds=payload["request_interval_seconds"],
            ),
        )
    result = BoundedSQLAgent(model, tool_profile=payload.get("tool_profile", "sql")).run(
        Task(**payload["task"]), Path(payload["database"]), documents=payload["documents"]
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
