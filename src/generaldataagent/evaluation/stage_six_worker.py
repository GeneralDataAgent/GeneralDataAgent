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
    )
    if payload.get("route_hash") and payload["route_hash"] != route["route_hash"]:
        raise ValueError("evaluation route changed after snapshot")
    model = build_evaluation_model(provider, route)
    result = BoundedSQLAgent(model, tool_profile=payload.get("tool_profile", "sql")).run(
        Task(**payload["task"]), Path(payload["database"]), documents=payload["documents"]
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
