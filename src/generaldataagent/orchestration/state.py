"""Serializable state for the first read-only vertical slice."""

from typing import Any, TypedDict


class PhaseOneState(TypedDict, total=False):
    task_id: str
    user_id: str
    tenant_id: str
    project_id: str
    user_query: str
    status: str
    semantic_context: dict[str, Any]
    task_plan: dict[str, Any]
    analysis_result: dict[str, Any]
    validation_result: dict[str, Any]
    delivery_package: dict[str, Any]
    errors: list[dict[str, Any]]
