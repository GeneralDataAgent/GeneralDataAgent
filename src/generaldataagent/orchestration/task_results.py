"""Read a completed metric delivery without resuming or executing its graph."""

import json
from typing import Literal
from uuid import UUID

from generaldataagent.artifacts.governed import GovernedArtifactStore, require_artifact_scope
from generaldataagent.contracts import DeliveryPackage, ValidationResult
from generaldataagent.contracts.models import ContractModel
from generaldataagent.orchestration.metric_analysis import MetricAnalysisRuntime


class MetricTaskResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    thread_id: UUID
    status: Literal["completed"] = "completed"
    validation: ValidationResult
    delivery: DeliveryPackage


def read_metric_result(runtime, context, guard) -> MetricTaskResult:
    """Caller has live creator authorization; checkpoint and artifacts must still agree.

    This performs producer evidence integrity checks, not independent truth validation.
    No model call, graph invoke/resume, budget reservation or data-source query occurs.
    """
    task_id, thread_id = context.record.task_id, context.record.thread_id
    require_artifact_scope(runtime.artifacts, context.actor)
    with runtime.checkpointer.execution(str(thread_id), context.actor, guard=guard):
        snapshot = runtime.graph.get_state({"configurable": {"thread_id": str(thread_id)}})
        state = snapshot.values
        if (
            snapshot.next
            or not state
            or state.get("status") != "completed"
            or state.get("task_id") != str(task_id)
            or state.get("actor_context") != context.actor.model_dump(mode="json")
            or state.get("metric_query") != context.query.model_dump(mode="json")
        ):
            raise ValueError("task_result_checkpoint_mismatch")
        validation = ValidationResult.model_validate(state["validation_result"])
        delivery = DeliveryPackage.model_validate(state["delivery_package"])
        if validation.task_id != task_id or validation.status != "pass":
            raise ValueError("task_result_validation_mismatch")
        expected = MetricAnalysisRuntime._deliver(state)["delivery_package"]
        if delivery.model_dump(mode="json") != expected:
            raise ValueError("task_result_delivery_mismatch")
        bodies = {}
        for ref in delivery.evidence:
            guard()
            if (
                ref.task_id != task_id
                or ref.uri != f"artifact://local/{task_id}/{ref.artifact_id}.json"
                or ref.artifact_type not in {"metric_query_result", "chart_spec", "llm_narrative"}
                or ref.uri in bodies
            ):
                raise ValueError("task_result_evidence_binding_mismatch")
            content = (
                runtime.artifacts.export_bytes(ref)
                if isinstance(runtime.artifacts, GovernedArtifactStore)
                else runtime.artifacts.read_bytes(ref)
            )
            if len(content) != ref.size_bytes:
                raise ValueError("task_result_evidence_size_mismatch")
            bodies[ref.uri] = (ref.artifact_type, json.loads(content))
        data_kind, data = bodies[delivery.table.data_artifact_uri]
        wire = delivery.model_dump(mode="json")
        chart_specs = delivery.chart_specs
        wire_charts = [chart.model_dump(mode="json") for chart in chart_specs]
        charts = [body for kind, body in bodies.values() if kind == "chart_spec"]
        if (
            data_kind != "metric_query_result"
            or any(
                chart.data_artifact_uri != delivery.table.data_artifact_uri for chart in chart_specs
            )
            or any(data.get(key) != wire[key] for key in ("columns", "rows", "row_count"))
            or len(charts) != len(chart_specs)
            or len({json.dumps(chart, sort_keys=True) for chart in charts}) != len(charts)
            or any(chart not in wire_charts for chart in charts)
        ):
            raise ValueError("task_result_evidence_content_mismatch")
        narratives = [body for kind, body in bodies.values() if kind == "llm_narrative"]
        if len(narratives) > 1 or bool(narratives) != ("narrative" in state["analysis_result"]):
            raise ValueError("task_result_narrative_mismatch")
        if narratives and (
            any(
                narratives[0].get(key) != wire[key]
                for key in ("summary", "insights", "limitations")
            )
            or narratives[0].get("model") != delivery.narrative_model
        ):
            raise ValueError("task_result_narrative_mismatch")
        guard()
        return MetricTaskResult(
            task_id=task_id, thread_id=thread_id, validation=validation, delivery=delivery
        )
