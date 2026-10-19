"""Authenticated pilot API. Approval decisions remain outside the HTTP surface."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, model_validator

from generaldataagent.business.actions import BusinessActionRuntime
from generaldataagent.business.feishu import RecallConfirmationRequired
from generaldataagent.business.schedules import ReportSchedule, ReportScheduler
from generaldataagent.contracts.business import ActionIntent
from generaldataagent.contracts.models import ContractModel


class ReceiverReconciliationRequest(ContractModel):
    message_id: str | None = Field(default=None, pattern=r"^om_[A-Za-z0-9]{1,128}$")
    pull_number: int | None = Field(default=None, gt=0)

    issue_number: int | None = Field(default=None, gt=0, le=9999999999, strict=True)

    @model_validator(mode="after")
    def exact_receiver_identifier(self):
        if sum(v is not None for v in (self.message_id, self.pull_number, self.issue_number)) != 1:
            raise ValueError("provide exactly one receiver identifier")
        return self

    @property
    def receiver_id(self):
        return (
            self.message_id
            if self.message_id is not None
            else str(self.pull_number if self.pull_number is not None else self.issue_number)
        )


def business_router(actions: BusinessActionRuntime, scheduler: ReportScheduler | None) -> APIRouter:
    router = APIRouter(prefix="/v1/business", tags=["Business drafts"])

    def token(request):
        return request.headers.get("authorization", "").split(" ", 1)[-1]

    def invoke(call, *args):
        try:
            return call(*args)
        except RecallConfirmationRequired as exc:
            raise HTTPException(
                status_code=409, detail="feishu_recall_confirmation_required"
            ) from exc
        except PermissionError as exc:
            # Details never include draft bodies or credentials.
            raise HTTPException(status_code=403, detail="business_access_denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="business_state_conflict") from exc
        except (TimeoutError, ConnectionError) as exc:
            raise HTTPException(status_code=503, detail="business_receiver_unavailable") from exc

    @router.post("/drafts")
    def propose(intent: ActionIntent, request: Request):
        return invoke(actions.propose, intent, token(request))

    @router.get("/drafts/{action_id}")
    def get(action_id: UUID, request: Request):
        return invoke(actions.get, action_id, token(request))

    @router.post("/drafts/{action_id}/execute")
    def execute(action_id: UUID, request: Request):
        return invoke(actions.execute, action_id, token(request))

    @router.post("/drafts/{action_id}/reconcile")
    def reconcile(action_id: UUID, body: ReceiverReconciliationRequest, request: Request):
        return invoke(actions.reconcile, action_id, token(request), body.receiver_id)

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
