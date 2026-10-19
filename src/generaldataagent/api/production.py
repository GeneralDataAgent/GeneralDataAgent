"""Production deployment factory: independent signed admission and a live kill switch."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from generaldataagent.api.app import create_app
from generaldataagent.security.admission import FileReleaseAdmission
from generaldataagent.security.identity import SQLiteSessionBroker


def create_released_app(
    *,
    admission: FileReleaseAdmission,
    session_broker: SQLiteSessionBroker,
    service_enabled: Callable[[], bool],
    **runtime_ports: Any,
) -> FastAPI:
    if not isinstance(session_broker, SQLiteSessionBroker):
        raise ValueError("released single-host deployment requires shared session revocation")
    if not admission.allowed():
        raise ValueError("independent release admission denied")
    if "production_mode" in runtime_ports:
        raise ValueError("released factory does not permit a development-mode override")

    business = runtime_ports.get("business_actions")
    if business is not None:
        from generaldataagent.business.production import QualifiedBusinessActionRuntime

        if isinstance(business, QualifiedBusinessActionRuntime) and (
            business.release.revision != admission.revision
            or business.release.environment != admission.environment
        ):
            raise ValueError("business and API release context must match")

    feedback = runtime_ports.get("feedback_registry")
    if feedback is not None:
        from generaldataagent.business.production_feedback import QualifiedFeedbackRegistry

        if isinstance(feedback, QualifiedFeedbackRegistry) and (
            feedback.release.revision != admission.revision
            or feedback.release.environment != admission.environment
        ):
            raise ValueError("feedback and API release context must match")

    def enabled() -> bool:
        return admission.allowed() and service_enabled() is True

    app = create_app(
        session_broker=session_broker,
        service_enabled=enabled,
        production_mode=True,
        **runtime_ports,
    )

    @app.middleware("http")
    async def gate_login(request: Request, call_next):
        if request.url.path.startswith("/auth/"):
            try:
                allowed = enabled()
            except Exception:
                allowed = False
            if not allowed:
                return JSONResponse(status_code=503, content={"detail": "release_admission_denied"})
        return await call_next(request)

    return app
