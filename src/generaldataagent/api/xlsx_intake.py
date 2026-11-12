"""Bounded raw-byte XLSX staging and preview; no confirmation or analysis surface."""

import asyncio
import re
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    XlsxFilePreview,
    XlsxIntakeLimit,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_intake_router(service: XlsxIntakeService | None, credentials: Callable) -> APIRouter:
    router = APIRouter()

    def configured():
        if service is None:
            raise HTTPException(status_code=503, detail="xlsx_intake_not_configured")
        return service

    def denied(exc):
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, XlsxIntakeLimit):
            return HTTPException(status_code=413, detail="xlsx_input_or_processing_limit")
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=404, detail="xlsx_file_unavailable")
        if isinstance(exc, XlsxIntakeConflict):
            return HTTPException(status_code=409, detail="xlsx_intake_conflict")
        if isinstance(exc, ValueError):
            return HTTPException(status_code=422, detail="xlsx_input_invalid")
        if isinstance(exc, (ClientDisconnect, TimeoutError)):
            return HTTPException(status_code=408, detail="xlsx_upload_incomplete_or_timeout")
        return HTTPException(status_code=503, detail="xlsx_intake_unavailable")

    async def collect(request, port, actor, token, max_bytes):
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(status_code=415, detail="xlsx_compression_not_supported")
        end = port.monotonic() + port.limits.max_seconds
        check = port.guard(actor, token, deadline=end)
        content = bytearray()
        async with asyncio.timeout(port.limits.max_seconds):
            async for chunk in request.stream():
                check()
                if len(content) + len(chunk) > max_bytes:
                    raise XlsxIntakeLimit("xlsx_request_byte_limit")
                content.extend(chunk)
        check()
        return bytes(content), end

    @router.post("/v1/analysis/files/xlsx", response_model=XlsxFilePreview, status_code=201)
    async def stage(
        request: Request,
        sheet: str | None = Query(default=None, min_length=1, max_length=31),
    ):
        port = configured()
        actor, token = credentials(request)
        if not re.fullmatch(
            re.escape(XLSX_MEDIA_TYPE), request.headers.get("content-type", ""), re.I
        ):
            raise HTTPException(status_code=415, detail="xlsx_ooxml_media_type_required")
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise HTTPException(status_code=422, detail="xlsx_idempotency_key_required")
        try:
            raw, end = await collect(request, port, actor, token, port.limits.max_bytes)
            return await run_in_threadpool(
                port.stage, raw, actor, token, key, sheet=sheet, deadline=end
            )
        except Exception as exc:
            raise denied(exc) from exc

    @router.get("/v1/analysis/files/xlsx/{file_id}", response_model=XlsxFilePreview)
    def preview(file_id: UUID, request: Request):
        port = configured()
        actor, token = credentials(request)
        try:
            return port.preview(file_id, actor, token)
        except Exception as exc:
            raise denied(exc) from exc

    return router
