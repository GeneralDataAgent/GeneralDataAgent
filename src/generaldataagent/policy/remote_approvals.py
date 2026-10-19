"""Bounded HTTPS approval client; the business process holds no review authority."""

import json
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

from generaldataagent.contracts import ApprovalGrant, ApprovalRequest, ToolRequest
from generaldataagent.contracts.approval import ApprovalDisposition
from generaldataagent.policy.approvals import ApprovalError, request_fingerprint


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPSApprovalTransport:
    """Operator-pinned origin, verified TLS, no redirect, retry or raw diagnostic output."""

    def __init__(self, *, origin: str, context: ssl.SSLContext | None = None):
        parts = urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("fixed HTTPS origin required")
        tls = context or ssl.create_default_context()
        if tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname:
            raise ValueError("verified TLS with hostname checks required")
        self.origin = origin.rstrip("/")
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=tls))

    def __call__(self, method: str, path: str, body: dict | None, token: str) -> dict:
        import re

        if method not in {"GET", "POST"} or not re.fullmatch(
            r"/v1/(?:approval-requests|approval-cancellations)(?:/[0-9a-f-]{36}(?:/(?:validate|consume))?)?",
            path,
        ):
            raise ValueError("unsupported_approval_operation")
        try:
            raw = (
                json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
                if body is not None
                else None
            )
            if raw is not None and len(raw) > 262144:
                raise ValueError("request too large")
            request = Request(
                self.origin + path,
                data=raw,
                method=method,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with self.opener.open(request, timeout=10) as response:
                content = response.read(262145)
                if not 200 <= response.status < 300 or len(content) > 262144:
                    raise ValueError("invalid response")
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("object required")
                return result
        except HTTPError:
            raise ApprovalError("remote_approval_denied") from None
        except Exception:
            raise ApprovalError("remote_approval_unavailable") from None


def _decoded(model, result):
    try:
        return model.model_validate(result)
    except Exception:
        raise ApprovalError("remote_approval_response_invalid") from None


class RemoteApprovalStore:
    """Requester credentials only. Real reviewers use the separate authenticated authority."""

    def __init__(self, *, transport: Callable, credential: Callable[[str], str]):
        self.transport, self.credential = transport, credential

    def create_request(
        self,
        request: ToolRequest,
        *,
        impact_scope,
        cost_estimate,
        classification,
        rollback_plan,
        expires_at,
    ) -> ApprovalRequest:
        metadata = {
            "impact_scope": impact_scope,
            "cost_estimate": cost_estimate,
            "classification": str(classification),
            "rollback_plan": rollback_plan,
            "expires_at": expires_at.isoformat(),
        }
        result = self.transport(
            "POST",
            "/v1/approval-requests",
            {"request": request.model_dump(mode="json"), **metadata},
            self.credential(request.user_id),
        )
        approval = _decoded(ApprovalRequest, result)
        expected = {
            "request_fingerprint": request_fingerprint(request),
            "task_id": request.task_id,
            "user_id": request.user_id,
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
            "tool_name": request.tool_name,
            "action": request.action,
            "resource": request.resource,
            "parameters": request.arguments,
            "data_domains": request.data_domains,
            "impact_scope": impact_scope,
            "cost_estimate": cost_estimate,
            "classification": classification,
            "rollback_plan": rollback_plan,
            "expires_at": expires_at,
        }
        if any(getattr(approval, key) != value for key, value in expected.items()):
            raise ApprovalError("remote_approval_binding_changed")
        return approval

    def _grant(self, operation, approval_id, request):
        result = self.transport(
            "POST",
            f"/v1/approval-requests/{approval_id}/{operation}",
            {"request": request.model_dump(mode="json")},
            self.credential(request.user_id),
        )
        grant = _decoded(ApprovalGrant, result)
        if (
            grant.approval_id != approval_id
            or grant.request_fingerprint != request_fingerprint(request)
            or grant.tenant_id != request.tenant_id
            or grant.approver_id == request.user_id
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise ApprovalError("remote_approval_grant_changed_or_expired")
        return grant

    def validate(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        return self._grant("validate", approval_id, request)

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        return self._grant("consume", approval_id, request)

    def approve(self, approval_id: UUID, *, approver_id: str):
        raise ApprovalError("independent_authenticated_reviewer_required")

    def reject(self, approval_id: UUID, *, reviewer_id: str, reason: str):
        raise ApprovalError("independent_authenticated_reviewer_required")

    def cancel(self, approval_id: UUID, *, requester_id: str, reason: str) -> ApprovalDisposition:
        result = self.transport(
            "POST", f"/v1/approval-cancellations/{approval_id}", {}, self.credential(requester_id)
        )
        disposition = _decoded(ApprovalDisposition, result)
        if (
            disposition.approval_id != approval_id
            or disposition.actor_id != requester_id
            or disposition.disposition != "cancelled"
        ):
            raise ApprovalError("remote_cancellation_changed")
        return disposition
