"""Authenticated pilot API. Approval decisions remain outside the HTTP surface."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from generaldataagent.business.actions import BusinessActionRuntime
from generaldataagent.business.schedules import ReportSchedule, ReportScheduler
from generaldataagent.contracts.business import ActionIntent


def business_router(actions: BusinessActionRuntime, scheduler: ReportScheduler | None) -> APIRouter:
    router = APIRouter(prefix="/v1/business", tags=["Business drafts"])

    def token(request):
        return request.headers.get("authorization", "").split(" ", 1)[-1]

    def invoke(call, *args):
        try:
            return call(*args)
        except PermissionError as exc:
            # Details never include draft bodies or credentials.
            raise HTTPException(status_code=403, detail="business_access_denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="business_state_conflict") from exc

    @router.post("/drafts")
    def propose(intent: ActionIntent, request: Request):
        return invoke(actions.propose, intent, token(request))

    @router.get("/drafts/{action_id}")
    def get(action_id: UUID, request: Request):
        return invoke(actions.get, action_id, token(request))

    @router.post("/drafts/{action_id}/execute")
    def execute(action_id: UUID, request: Request):
        return invoke(actions.execute, action_id, token(request))

    @router.post("/drafts/{action_id}/cancel")
    def cancel(action_id: UUID, request: Request):
        return invoke(actions.cancel, action_id, token(request))

    if scheduler is not None:

        @router.post("/schedules")
        def register(spec: ReportSchedule, request: Request):
            return invoke(scheduler.register, spec, token(request))

        @router.post("/schedules/{schedule_id}/tick")
        def tick(schedule_id: UUID, request: Request):
            return invoke(scheduler.tick, schedule_id, token(request))

        @router.post("/schedules/{schedule_id}/cancel")
        def cancel_schedule(schedule_id: UUID, request: Request):
            return invoke(scheduler.cancel, schedule_id, token(request))

    return router
