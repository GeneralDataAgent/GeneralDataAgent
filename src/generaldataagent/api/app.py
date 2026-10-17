"""HTTP surface for health, contracts, and certified metric analysis."""

from collections.abc import Callable
from time import monotonic
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from generaldataagent import __version__
from generaldataagent.agents import SPECIALIST_SPECS
from generaldataagent.catalog import MetricNotFoundError
from generaldataagent.contracts import (
    ActorContext,
    DatasetSnapshot,
    ExperimentSpec,
    HumanReview,
    MetricQuery,
    NaturalLanguageMetricQuery,
    TaskPlan,
)
from generaldataagent.data_science.sandbox import SandboxError
from generaldataagent.data_science.tracking import ExperimentTrackingError
from generaldataagent.data_science.workbench import DataScienceWorkbench, ScienceGateError
from generaldataagent.evaluation import DuplicateReviewError, LocalHumanReviewStore
from generaldataagent.evaluation.scoped_reviews import ScopedHumanReviewStore
from generaldataagent.observability.alerts import CriticalAlertRouter
from generaldataagent.observability.slo import ServiceSLO
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.policy.resume import PendingActionCoordinator
from generaldataagent.security.identity import SessionBroker
from generaldataagent.security.login import OIDCLoginFlow
from generaldataagent.tools import QueryExecutionError


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    version: str


class ScienceExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: DatasetSnapshot
    experiment: ExperimentSpec


def create_app(
    metric_runtime: MetricAnalysisRuntime | None = None,
    review_store: LocalHumanReviewStore | ScopedHumanReviewStore | None = None,
    science_workbench: DataScienceWorkbench | None = None,
    session_broker: SessionBroker | None = None,
    production_mode: bool = False,
    service_enabled: Callable[[], bool] | None = None,
    pending_actions: PendingActionCoordinator | None = None,
    slo: ServiceSLO | None = None,
    oidc_login: OIDCLoginFlow | None = None,
    alerts: CriticalAlertRouter | None = None,
) -> FastAPI:
    if pending_actions is not None and (
        session_broker is None or pending_actions.broker is not session_broker
    ):
        raise ValueError("action coordinator must share the API identity broker")
    if oidc_login is not None and oidc_login.adapter.broker is not session_broker:
        raise ValueError("login flow must share the API identity broker")
    if production_mode and session_broker is None:
        raise ValueError("production mode requires an identity broker")
    if production_mode and metric_runtime is not None and not metric_runtime.production_mode:
        raise ValueError("production API requires a production-governed analysis runtime")
    if production_mode and science_workbench is not None and not science_workbench.production_mode:
        raise ValueError("production API requires a production-governed science workbench")
    application = FastAPI(
        title="GeneralDataAgent",
        version=__version__,
        description="Governed multi-agent foundation for enterprise data work.",
    )
    application.state.metric_runtime = metric_runtime
    application.state.review_store = review_store
    application.state.science_workbench = science_workbench

    @application.middleware("http")
    async def enforce_identity(request: Request, call_next):
        if request.url.path.startswith("/v1/") and service_enabled is not None:
            try:
                enabled = service_enabled() is True
            except Exception:
                enabled = False
            if not enabled:
                return JSONResponse(status_code=503, content={"detail": "service_disabled"})
        if session_broker is not None and request.url.path.startswith("/v1/"):
            authorization = request.headers.get("authorization", "")
            try:
                scheme, token = authorization.split(" ", 1)
                if scheme.lower() != "bearer" or not token:
                    raise ValueError("missing bearer token")
                identity = session_broker.authenticate(token)
            except (ValueError, PermissionError):
                return JSONResponse(status_code=401, content={"detail": "invalid_session"})
            scope = {
                "x-user-id": identity.user_id,
                "x-tenant-id": identity.tenant_id,
                "x-project-id": identity.project_id,
            }
            if any(request.headers.get(key) != value for key, value in scope.items()):
                return JSONResponse(status_code=403, content={"detail": "identity_scope_mismatch"})
            domains = request.headers.getlist("x-data-domain")
            if not domains or not set(domains) <= identity.data_domains:
                return JSONResponse(status_code=403, content={"detail": "data_domain_denied"})
            if request.url.path.startswith("/v1/reviews") and not isinstance(
                application.state.review_store, ScopedHumanReviewStore
            ):
                return JSONResponse(status_code=403, content={"detail": "unscoped_review_store"})
            path = request.url.path
            required = None
            if path.startswith("/v1/analysis/"):
                required = "analysis:run"
            elif path.startswith("/v1/science/"):
                required = "science:run"
            elif path.startswith("/v1/actions/"):
                required = "actions:resume"
            elif path.startswith("/v1/operations/"):
                required = "operations:read"
            elif path.startswith("/v1/reviews"):
                required = "reviews:submit" if request.method == "POST" else "reviews:read"
            if required is not None and required not in identity.permissions:
                return JSONResponse(status_code=403, content={"detail": "permission_denied"})
            request.state.identity = identity
        return await call_next(request)

    @application.middleware("http")
    async def observe_service(request: Request, call_next):
        started = monotonic()
        available = False
        try:
            response = await call_next(request)
            available = response.status_code < 500
            return response
        finally:
            if slo is not None and request.url.path.startswith("/v1/"):
                slo.record(duration_seconds=monotonic() - started, available=available)

    def authenticated_actor(request: Request) -> ActorContext:
        identity = getattr(request.state, "identity", None)
        if identity is None:
            raise HTTPException(status_code=401, detail="authenticated_identity_required")
        return ActorContext(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            data_domains=tuple(identity.data_domains),
        )

    def register_review_task(result: dict[str, Any], actor: ActorContext) -> None:
        if isinstance(review_store, ScopedHumanReviewStore) and result.get("task_id"):
            review_store.register_task(UUID(str(result["task_id"])), actor)

    @application.get("/auth/login")
    def begin_login():
        if oidc_login is None:
            raise HTTPException(status_code=503, detail="sso_not_configured")
        try:
            url, cookie = oidc_login.begin()
        except PermissionError as exc:
            raise HTTPException(status_code=503, detail="login_unavailable") from exc
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            "gda_login",
            cookie,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/auth",
            max_age=300,
        )
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return response

    @application.get("/auth/callback")
    def finish_login(request: Request, state: str = "", code: str = ""):
        if oidc_login is None:
            raise HTTPException(status_code=503, detail="sso_not_configured")
        try:
            token = oidc_login.finish(
                state=state, code=code, cookie=request.cookies.get("gda_login", "")
            )
            response = JSONResponse({"access_token": token, "token_type": "Bearer"})
        except PermissionError:
            response = JSONResponse({"detail": "oidc_login_failed"}, status_code=401)
        response.delete_cookie(
            "gda_login", path="/auth", secure=True, httponly=True, samesite="lax"
        )
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return response

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @application.get("/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "agents": [spec.name for spec in SPECIALIST_SPECS],
            "certified_metric_analysis": metric_runtime is not None,
            "human_review": review_store is not None,
            "data_science_workbench": science_workbench is not None,
            "production_writes": False,
            "arbitrary_shell": False,
            "implemented_controls": [
                "versioned_contracts",
                "policy_engine",
                "read_only_sql_guard",
                "artifact_hashing",
                "validation_gate",
                "fixed_template_science_sandbox",
            ],
        }

    @application.get("/v1/operations/slo")
    def service_slo() -> dict[str, Any]:
        if slo is None:
            raise HTTPException(status_code=503, detail="slo_not_configured")
        return slo.snapshot()

    @application.get("/v1/operations/alerts")
    def alert_measurements() -> dict[str, Any]:
        if alerts is None:
            raise HTTPException(status_code=503, detail="alerts_not_configured")
        return alerts.measurements()

    @application.post("/v1/science/experiments")
    def run_science_experiment(
        request: ScienceExperimentRequest,
        x_user_id: Annotated[str, Header(min_length=1)],
        x_tenant_id: Annotated[str, Header(min_length=1)],
        x_project_id: Annotated[str, Header(min_length=1)],
        x_data_domain: Annotated[str, Header(min_length=1)],
        x_trace_id: Annotated[str | None, Header(min_length=1)] = None,
    ) -> dict[str, Any]:
        workbench = application.state.science_workbench
        if workbench is None:
            raise HTTPException(
                status_code=503,
                detail="Data-science workbench is not configured for this deployment.",
            )
        try:
            report = workbench.run_experiment(
                snapshot=request.snapshot,
                spec=request.experiment,
                user_id=x_user_id,
                tenant_id=x_tenant_id,
                project_id=x_project_id,
                data_domain=x_data_domain,
                trace_id=x_trace_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ExperimentTrackingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SandboxError, ScienceGateError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report.model_dump(mode="json")

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
            register_review_task(result, actor)
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
            register_review_task(result, actor)
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

    @application.post("/v1/actions/{thread_id}/resume")
    def resume_action(thread_id: str, request: Request) -> dict[str, Any]:
        if pending_actions is None:
            raise HTTPException(status_code=503, detail="pending_actions_not_configured")
        authenticated_actor(request)
        token = request.headers["authorization"].split(" ", 1)[1]
        try:
            result = pending_actions.resume(thread_id, token)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"thread_id": thread_id, "status": "completed", "result": result}

    @application.post("/v1/reviews")
    def submit_review(review: HumanReview, request: Request) -> dict[str, Any]:
        store = application.state.review_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Human review storage is not configured for this deployment.",
            )
        try:
            if isinstance(store, ScopedHumanReviewStore):
                stored = store.submit_scoped(review, authenticated_actor(request))
            else:
                stored = store.submit(review)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except DuplicateReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return stored.model_dump(mode="json")

    @application.get("/v1/reviews/{task_id}/summary")
    def review_summary(task_id: UUID, request: Request) -> dict[str, Any]:
        store = application.state.review_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Human review storage is not configured for this deployment.",
            )
        try:
            summary = (
                store.summarize_scoped(task_id, authenticated_actor(request))
                if isinstance(store, ScopedHumanReviewStore)
                else store.summarize(task_id)
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if summary.review_count == 0:
            raise HTTPException(status_code=404, detail="No human reviews exist for this task.")
        return summary.model_dump(mode="json")

    return application


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("generaldataagent.api.app:app", host="127.0.0.1", port=8000)
