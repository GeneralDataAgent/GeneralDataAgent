"""Separately deployed, authenticated approval authority; no grants in the business API."""

from collections.abc import Callable
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import AwareDatetime, Field

from generaldataagent.contracts import AuditEvent, ToolRequest
from generaldataagent.contracts.approval import ApprovalRequest
from generaldataagent.contracts.models import ContractModel
from generaldataagent.contracts.types import ArtifactClassification
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.policy.approvals import (
    SQLiteApprovalStore,
    request_fingerprint,
)
from generaldataagent.security.admission import FileReleaseAdmission
from generaldataagent.security.dlp import TextDLPScanner
from generaldataagent.security.identity import SessionBroker, SQLiteSessionBroker


class ApprovalCreation(ContractModel):
    request: ToolRequest
    impact_scope: str = Field(min_length=1, max_length=2000)
    cost_estimate: str = Field(min_length=1, max_length=1000)
    classification: ArtifactClassification
    rollback_plan: str = Field(min_length=1, max_length=4000)
    expires_at: AwareDatetime


class ApprovalExecution(ContractModel):
    request: ToolRequest


class ReviewDecision(ContractModel):
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmed: bool = Field(strict=True)
    reason: str = Field(default="Reviewed exact request", min_length=1, max_length=2000)


def create_approval_service(
    *,
    store: SQLiteApprovalStore,
    broker: SessionBroker,
    reviewer_admitted: Callable[[object, ApprovalRequest], bool],
    audit,
    admission: FileReleaseAdmission | None = None,
    service_enabled: Callable[[], bool] = lambda: True,
    delegation_allowed: Callable[[object, str], bool] = lambda identity, requester: False,
    production_mode: bool = True,
) -> FastAPI:
    if production_mode and (
        not isinstance(broker, SQLiteSessionBroker)
        or not isinstance(audit, VerifiedAuditSink)
        or not isinstance(admission, FileReleaseAdmission)
    ):
        raise ValueError(
            "production approval service requires release admission, "
            "shared sessions and independent audit"
        )
    app = FastAPI(title="GeneralDataAgent independent approval authority")

    from importlib.resources import files

    from fastapi.responses import Response

    from generaldataagent.api.business_ui import HEADERS

    @app.get("/review")
    def review_page():
        return Response(
            files("generaldataagent.api").joinpath("web/review.html").read_bytes(),
            media_type="text/html",
            headers=HEADERS,
        )

    @app.get("/review/console.js")
    def review_script():
        return Response(
            files("generaldataagent.api").joinpath("web/review.js").read_bytes(),
            media_type="text/javascript",
            headers=HEADERS,
        )

    @app.get("/review/console.css")
    def review_styles():
        return Response(
            files("generaldataagent.api").joinpath("web/business.css").read_bytes(),
            media_type="text/css",
            headers=HEADERS,
        )

    @app.middleware("http")
    async def bounds(request: Request, call_next):
        from fastapi.responses import JSONResponse

        try:
            enabled = service_enabled() is True and (admission is None or admission.allowed())
        except Exception:
            enabled = False
        if not enabled:
            return JSONResponse(status_code=503, content={"detail": "approval_service_disabled"})
        # Cache the bounded body for FastAPI; terminate streams as soon as the cap is exceeded.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 262144:
                return JSONResponse(
                    status_code=413, content={"detail": "approval_request_too_large"}
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response

    def actor(request, permission):
        try:
            scheme, token = request.headers.get("authorization", "").split(" ", 1)
            if scheme.lower() != "bearer" or not token:
                raise ValueError("invalid authentication")
            identity = broker.authenticate(token)
            if permission not in identity.permissions or identity.business_constraint is not None:
                raise PermissionError("approval_permission_denied")
            return identity
        except (ValueError, PermissionError):
            raise HTTPException(status_code=403, detail="approval_access_denied") from None

    def context(identity, request):
        if (
            (identity.tenant_id, identity.project_id) != (request.tenant_id, request.project_id)
            or not request.data_domains
            or not set(request.data_domains) <= identity.data_domains
        ):
            raise HTTPException(status_code=403, detail="approval_scope_denied")

    def requester(identity, request):
        context(identity, request)
        if request.user_id != identity.user_id and not (
            "approvals:delegate" in identity.permissions
            and delegation_allowed(identity, request.user_id) is True
        ):
            raise HTTPException(status_code=403, detail="approval_requester_denied")

    def reviewer(identity, request):
        context(identity, request)
        if identity.user_id == request.user_id or reviewer_admitted(identity, request) is not True:
            raise HTTPException(status_code=403, detail="independent_reviewer_required")

    def event(identity, request, name):
        try:
            audit.emit(
                AuditEvent(
                    task_id=request.task_id,
                    trace_id=str(request.approval_id)
                    if isinstance(request, ApprovalRequest)
                    else str(request.request_id),
                    event_type=name,
                    actor_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    payload={
                        "request_fingerprint": request.request_fingerprint
                        if isinstance(request, ApprovalRequest)
                        else request_fingerprint(request)
                    },
                )
            )
        except Exception:
            raise HTTPException(
                status_code=503, detail="independent_approval_audit_unavailable"
            ) from None

    def invoke(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except (PermissionError, ValueError):
            raise HTTPException(status_code=409, detail="approval_state_conflict") from None

    @app.post("/v1/approval-requests")
    def create(body: ApprovalCreation, request: Request):
        identity = actor(request, "approvals:request")
        requester(identity, body.request)
        invoke(TextDLPScanner().check, body.model_dump_json().encode())
        event(identity, body.request, "approval.requesting")
        requester(actor(request, "approvals:request"), body.request)
        result = invoke(
            store.create_request,
            body.request,
            **body.model_dump(exclude={"request", "schema_version"}, mode="python"),
        )
        event(identity, result, "approval.requested")
        return result

    @app.get("/v1/approval-requests")
    def list_requests(
        request: Request,
        before: int | None = Query(default=None, gt=0),
        limit: int = Query(default=25, ge=1, le=100),
    ):
        identity = actor(request, "approvals:review")
        page = store.review_page(
            tenant=identity.tenant_id,
            project=identity.project_id,
            domains=identity.data_domains,
            before=before,
            limit=limit,
        )
        # A reviewer sees only requests admitted by the operator's current enrollment policy.
        page["items"] = [
            record
            for record in page["items"]
            if record.request.user_id != identity.user_id
            and reviewer_admitted(identity, record.request) is True
        ]
        return page

    @app.get("/v1/approval-requests/{approval_id}")
    def inspect(approval_id: UUID, request: Request):
        identity = actor(request, "approvals:request")
        record = invoke(store.inspect, approval_id)
        requester(identity, record.request)
        return record

    @app.post("/v1/approval-requests/{approval_id}/{operation}")
    def execute(approval_id: UUID, operation: str, body: ApprovalExecution, request: Request):
        if operation not in {"validate", "consume"}:
            raise HTTPException(status_code=404, detail="unknown_approval_operation")
        identity = actor(request, "approvals:request")
        requester(identity, body.request)
        original = invoke(store.inspect, approval_id).request
        requester(identity, original)
        if original.request_fingerprint != request_fingerprint(body.request):
            raise HTTPException(status_code=409, detail="approved_request_changed")
        event(identity, original, "approval." + operation + "_requested")
        requester(actor(request, "approvals:request"), original)
        result = invoke(getattr(store, operation), approval_id, body.request)
        event(
            identity,
            original,
            "approval.consumed" if operation == "consume" else "approval.validated",
        )
        return result

    @app.post("/v1/approval-reviews/{approval_id}/{decision}")
    def decide(approval_id: UUID, decision: str, body: ReviewDecision, request: Request):
        if decision not in {"approve", "reject"}:
            raise HTTPException(status_code=404, detail="unknown_review_decision")
        identity = actor(request, "approvals:review")
        original = invoke(store.inspect, approval_id).request
        reviewer(identity, original)
        if body.confirmed is not True or body.request_fingerprint != original.request_fingerprint:
            raise HTTPException(status_code=409, detail="exact_review_confirmation_required")
        event(identity, original, "approval.review_requested")
        reviewer(actor(request, "approvals:review"), original)
        if decision == "approve":
            result = invoke(store.approve, approval_id, approver_id=identity.user_id)
        else:
            result = invoke(
                store.reject, approval_id, reviewer_id=identity.user_id, reason=body.reason
            )
        event(identity, original, "approval.reviewed")
        return result

    @app.post("/v1/approval-cancellations/{approval_id}")
    def cancel(approval_id: UUID, request: Request):
        identity = actor(request, "approvals:request")
        original = invoke(store.inspect, approval_id).request
        requester(identity, original)
        event(identity, original, "approval.cancel_requested")
        requester(actor(request, "approvals:request"), original)
        result = invoke(
            store.cancel,
            approval_id,
            requester_id=original.user_id,
            reason="Requester cancelled exact action",
        )
        event(identity, original, "approval.cancelled")
        return result

    return app
