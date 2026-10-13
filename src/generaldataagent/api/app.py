"""HTTP surface for health, contracts, and certified metric analysis."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from generaldataagent import __version__
from generaldataagent.agents import SPECIALIST_SPECS
from generaldataagent.catalog import MetricNotFoundError
from generaldataagent.contracts import (
    ActorContext,
    HumanReview,
    MetricQuery,
    NaturalLanguageMetricQuery,
    TaskPlan,
)
from generaldataagent.evaluation import DuplicateReviewError, LocalHumanReviewStore
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import QueryExecutionError


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    version: str


def create_app(
    metric_runtime: MetricAnalysisRuntime | None = None,
    review_store: LocalHumanReviewStore | None = None,
) -> FastAPI:
    application = FastAPI(
        title="GeneralDataAgent",
        version=__version__,
        description="Governed multi-agent foundation for enterprise data work.",
    )
    application.state.metric_runtime = metric_runtime
    application.state.review_store = review_store

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @application.get("/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "agents": [spec.name for spec in SPECIALIST_SPECS],
            "certified_metric_analysis": metric_runtime is not None,
            "human_review": review_store is not None,
            "production_writes": False,
            "arbitrary_shell": False,
            "implemented_controls": [
                "versioned_contracts",
                "policy_engine",
                "read_only_sql_guard",
                "artifact_hashing",
                "validation_gate",
            ],
        }

    @application.post("/v1/contracts/task-plan/validate")
    def validate_task_plan(plan: TaskPlan) -> dict[str, Any]:
        return {
            "valid": True,
            "schema_version": plan.schema_version,
            "task_id": str(plan.task_id),
            "step_count": len(plan.steps),
        }

    @application.post("/v1/analysis/metrics")
    def analyze_metric(
        query: MetricQuery,
        x_user_id: Annotated[str, Header(min_length=1)],
        x_tenant_id: Annotated[str, Header(min_length=1)],
        x_project_id: Annotated[str, Header(min_length=1)],
        x_data_domain: Annotated[list[str], Header(min_length=1)],
    ) -> dict[str, Any]:
        runtime = application.state.metric_runtime
        if runtime is None:
            raise HTTPException(
                status_code=503,
                detail="Certified metric analysis is not configured for this deployment.",
            )
        actor = ActorContext(
            user_id=x_user_id,
            tenant_id=x_tenant_id,
            project_id=x_project_id,
            data_domains=tuple(x_data_domain),
        )
        try:
            result = runtime.run(query, actor)
        except MetricNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (QueryExecutionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "task_id": result["task_id"],
            "status": result["status"],
            "validation": result.get("validation_result"),
            "delivery": result.get("delivery_package"),
            "errors": result.get("errors", []),
        }

    @application.post("/v1/analysis/questions")
    def analyze_question(
        query: NaturalLanguageMetricQuery,
        x_user_id: Annotated[str, Header(min_length=1)],
        x_tenant_id: Annotated[str, Header(min_length=1)],
        x_project_id: Annotated[str, Header(min_length=1)],
        x_data_domain: Annotated[list[str], Header(min_length=1)],
    ) -> dict[str, Any]:
        runtime = application.state.metric_runtime
        if runtime is None:
            raise HTTPException(
                status_code=503,
                detail="Certified metric analysis is not configured for this deployment.",
            )
        actor = ActorContext(
            user_id=x_user_id,
            tenant_id=x_tenant_id,
            project_id=x_project_id,
            data_domains=tuple(x_data_domain),
        )
        try:
            result = runtime.analyze_question(query, actor)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (QueryExecutionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "task_id": result.get("task_id"),
            "status": result["status"],
            "resolution": result.get("resolution"),
            "validation": result.get("validation_result"),
            "delivery": result.get("delivery_package"),
            "errors": result.get("errors", []),
        }

    @application.post("/v1/reviews")
    def submit_review(review: HumanReview) -> dict[str, Any]:
        store = application.state.review_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Human review storage is not configured for this deployment.",
            )
        try:
            stored = store.submit(review)
        except DuplicateReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return stored.model_dump(mode="json")

    @application.get("/v1/reviews/{task_id}/summary")
    def review_summary(task_id: UUID) -> dict[str, Any]:
        store = application.state.review_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Human review storage is not configured for this deployment.",
            )
        summary = store.summarize(task_id)
        if summary.review_count == 0:
            raise HTTPException(status_code=404, detail="No human reviews exist for this task.")
        return summary.model_dump(mode="json")

    return application


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("generaldataagent.api.app:app", host="127.0.0.1", port=8000)
