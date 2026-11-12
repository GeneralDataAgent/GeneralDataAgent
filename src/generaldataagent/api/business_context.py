"""Owner-private explicit business definitions and project background."""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Path, Query, Request

from generaldataagent.memory.business_context import (
    BusinessContextConflict,
    BusinessContextRecord,
    BusinessContextState,
    BusinessContextUnavailable,
    ConfirmBusinessContext,
)
from generaldataagent.orchestration.business_context import BusinessContextService
from generaldataagent.orchestration.task_queue import TaskQueueConflict


def business_context_router(
    service: BusinessContextService | None, credentials: Callable
) -> APIRouter:
    router = APIRouter()

    def invoke(request, action):
        if service is None:
            raise HTTPException(status_code=503, detail="business_context_not_configured")
        actor, token = credentials(request)
        try:
            return action(service, actor, token)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail="business_context_unavailable") from exc
        except (BusinessContextConflict, TaskQueueConflict) as exc:
            detail = str(exc) or "business_context_conflict"
            raise HTTPException(status_code=409, detail=detail) from exc
        except (BusinessContextUnavailable, ValueError) as exc:
            raise HTTPException(status_code=503, detail="business_context_unavailable") from exc

    path = "/v1/analysis/context"

    @router.get(path, response_model=BusinessContextState)
    def current(request: Request):
        return invoke(request, lambda s, a, t: s.current(a, t))

    @router.put(path, response_model=BusinessContextState)
    def confirm(request: Request, body: ConfirmBusinessContext):
        return invoke(request, lambda s, a, t: s.confirm(a, t, body))

    @router.delete(path, response_model=BusinessContextState)
    def revoke(request: Request, expected_version: int = Query(ge=0, le=2**31 - 1)):
        return invoke(request, lambda s, a, t: s.revoke(a, t, expected_version))

    @router.get(path + "/versions/{version}", response_model=BusinessContextRecord)
    def history(request: Request, version: int = Path(ge=1, le=2**31 - 1)):
        return invoke(request, lambda s, a, t: s.history(a, t, version))

    return router
