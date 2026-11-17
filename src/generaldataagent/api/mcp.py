"""Development product entry for one admitted, read-only MCP tool."""

import re
from collections.abc import Callable
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request

from generaldataagent.connectors.mcp import (
    McpAuthFailed,
    McpProtocolError,
    McpRequestRejected,
    McpResponseDenied,
    McpResponseTooLarge,
    McpSchemaDrift,
    McpServerUnavailable,
    McpSessionBusy,
    McpSessionScopeError,
    McpTimeout,
    McpToolConflict,
    McpToolError,
    McpToolNotAdmitted,
    McpToolNotFound,
    McpVersionMismatch,
)
from generaldataagent.contracts import McpReadEvidence, McpReadRequest
from generaldataagent.orchestration.mcp import McpReadService
from generaldataagent.policy import ApprovalError
from generaldataagent.tools.gateway import PolicyDeniedError

_REMOTE_TOOL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TRACE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def mcp_read_router(service: McpReadService | None, credentials: Callable) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/analysis/mcp/{remote_tool}/read",
        response_model=McpReadEvidence,
    )
    async def read(
        remote_tool: str,
        body: McpReadRequest,
        request: Request,
        approval_id: Annotated[UUID | None, Header(alias="x-approval-id")] = None,
    ):
        if service is None:
            raise HTTPException(status_code=503, detail="mcp_read_not_configured")
        if not _REMOTE_TOOL.fullmatch(remote_tool):
            raise HTTPException(status_code=422, detail="mcp_tool_name_invalid")
        actor, _token = credentials(request)
        trace_id = request.headers.get("x-trace-id", uuid4().hex)
        if not _TRACE.fullmatch(trace_id):
            raise HTTPException(status_code=422, detail="mcp_trace_id_invalid")
        try:
            return await service.read_async(
                actor=actor,
                task_id=body.task_id,
                remote_tool=remote_tool,
                arguments=body.arguments,
                trace_id=trace_id,
                approval_id=approval_id,
            )
        except (PolicyDeniedError, ApprovalError):
            raise HTTPException(status_code=403, detail="mcp_policy_denied") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="mcp_permission_denied") from None
        except McpToolNotAdmitted:
            raise HTTPException(status_code=404, detail="mcp_tool_not_admitted") from None
        except (McpToolNotFound, McpSchemaDrift, McpToolConflict, McpVersionMismatch):
            raise HTTPException(status_code=409, detail="mcp_contract_changed") from None
        except (McpSessionScopeError, McpSessionBusy):
            raise HTTPException(status_code=409, detail="mcp_session_unavailable") from None
        except McpResponseTooLarge:
            raise HTTPException(status_code=413, detail="mcp_response_too_large") from None
        except McpResponseDenied:
            raise HTTPException(status_code=422, detail="mcp_response_denied") from None
        except McpRequestRejected:
            raise HTTPException(status_code=422, detail="mcp_request_rejected") from None
        except McpTimeout:
            raise HTTPException(status_code=504, detail="mcp_timeout") from None
        except McpAuthFailed:
            raise HTTPException(status_code=503, detail="mcp_auth_failed") from None
        except (McpProtocolError, McpServerUnavailable, McpToolError):
            raise HTTPException(status_code=502, detail="mcp_upstream_failure") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="mcp_input_invalid") from None

    return router
