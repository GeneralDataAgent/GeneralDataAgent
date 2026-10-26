"""One answer-blind evaluation worker. Only repository-owned code is executed."""

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.evaluation.stage_six_models import (
    build_evaluation_model,
    resolve_evaluation_model,
)


def _experience_port(payload: dict):
    """Build the reviewed-experience reader, or a port that keeps failing visibly.

    Experience is advisory. If the snapshot cannot be opened or its actor scope
    is malformed, the case must still run and report the failure at the step
    that asked for hints; turning it into worker_error would attribute an
    infrastructure fault to the task under test. Returning ``None`` instead
    would silently look like the off arm, so the port stays present and raises.
    """
    if not payload.get("experience_database"):
        return None
    try:
        from generaldataagent.business.experience import ExperienceStore, experience_hints
        from generaldataagent.contracts import ActorContext

        path = Path(payload["experience_database"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != payload.get("experience_store_hash"):
            raise ValueError("experience_snapshot_hash_mismatch")
        store = ExperienceStore(
            path,
            clock=lambda: datetime.now(UTC),
            read_only=True,
        )
        actor = ActorContext.model_validate(payload["experience_actor"])
        return experience_hints(store, actor)
    except Exception as exc:
        # The name ``exc`` is cleared when this block exits; keep the object.
        failure = exc

        def unavailable(failure_class: str, tool: str):
            raise failure

        return unavailable


def main():
    payload = json.loads(sys.stdin.read())
    provider, route = resolve_evaluation_model(
        app_type=payload["app_type"],
        provider_id=payload["provider_id"],
        model_override=payload.get("model_override"),
        transport=payload.get("transport", "configured"),
        thinking_mode=payload.get("thinking_mode", "default"),
        json_mode=payload.get("json_mode", False),
        max_output_tokens=payload.get("max_output_tokens"),
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
            max_seconds=payload.get("max_seconds", 120),
        )
    experience = _experience_port(payload)
    recorder = None
    if payload.get("receipt_config") is not None:
        from uuid import UUID

        from generaldataagent.artifacts import LocalArtifactStore
        from generaldataagent.contracts import ActorContext
        from generaldataagent.evaluation.receipts import EvaluationReceiptRecorder

        config = payload["receipt_config"]
        recorder = EvaluationReceiptRecorder(
            artifacts=LocalArtifactStore(Path(config["artifact_root"])),
            actor=ActorContext.model_validate(config["actor"]),
            task_id=UUID(config["task_id"]),
            trace_id=config["trace_id"],
            task=Task(**payload["task"]),
            database=Path(payload["database"]),
            documents=payload["documents"],
            profile=payload.get("tool_profile", "sql"),
        )
    result = BoundedSQLAgent(
        model,
        tool_profile=payload.get("tool_profile", "sql"),
        max_tokens=payload.get("max_tokens"),
        max_steps=payload.get("max_steps", 6),
        max_seconds=payload.get("max_seconds", 120),
        max_repairs=payload.get("max_repairs", 3),
        experience=experience,
        max_experience=payload.get("max_experience", 2),
    ).run(
        Task(**payload["task"]),
        Path(payload["database"]),
        documents=payload["documents"],
        **({"receipt_recorder": recorder} if recorder is not None else {}),
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
