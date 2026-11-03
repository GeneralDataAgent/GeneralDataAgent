"""Bounded raw-byte CSV intake; paths, archives and unconfirmed profiling are absent."""

import asyncio
import re
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from generaldataagent.datasets.csv_intake import CsvIntakeService
from generaldataagent.datasets.csv_intake_contracts import (
    ConfirmCsvText,
    CsvFilePreview,
    CsvProfileResult,
)
from generaldataagent.datasets.csv_intake_store import CsvIntakeConflict
from generaldataagent.datasets.csv_parser import CsvIntakeError, CsvIntakeLimit
from generaldataagent.policy.budgets import BudgetExceededError


def csv_intake_router(service: CsvIntakeService | None, credentials: Callable) -> APIRouter:
    router = APIRouter()

    def configured():
        if service is None:
            raise HTTPException(status_code=503, detail="csv_intake_not_configured")
        return service

    def denied(exc):
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, CsvIntakeLimit):
            return HTTPException(status_code=413, detail="csv_input_or_processing_limit")
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=404, detail="csv_file_unavailable")
        if isinstance(exc, (CsvIntakeConflict, BudgetExceededError)):
            return HTTPException(status_code=409, detail="csv_intake_conflict")
        if isinstance(exc, (CsvIntakeError, ValueError)):
            return HTTPException(status_code=422, detail="csv_input_invalid")
        if isinstance(exc, (ClientDisconnect, TimeoutError)):
            return HTTPException(status_code=408, detail="csv_upload_incomplete_or_timeout")
        return HTTPException(status_code=503, detail="csv_intake_unavailable")

    async def collect(request, port, actor, token, max_bytes):
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(status_code=415, detail="csv_compression_not_supported")
        end = port.monotonic() + port.limits.max_seconds
        check = port.guard(actor, token, deadline=end)
        content = bytearray()
        async with asyncio.timeout(port.limits.max_seconds):
            async for chunk in request.stream():
                check()
                if len(content) + len(chunk) > max_bytes:
                    raise CsvIntakeLimit("csv_request_byte_limit")
                content.extend(chunk)
        check()
        return bytes(content), end

    @router.post("/v1/analysis/files/csv", response_model=CsvFilePreview, status_code=201)
    async def stage(request: Request):
        port = configured()
        actor, token = credentials(request)
        if not re.fullmatch(
            r"text/csv(?:\s*;\s*charset\s*=\s*utf-8)?",
            request.headers.get("content-type", ""),
            re.I,
        ):
            raise HTTPException(status_code=415, detail="csv_utf8_media_type_required")
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise HTTPException(status_code=422, detail="csv_idempotency_key_required")
        try:
            raw, end = await collect(request, port, actor, token, port.limits.max_bytes)
            return await run_in_threadpool(port.stage, raw, actor, token, key, deadline=end)
        except Exception as exc:
            raise denied(exc) from exc

    @router.get("/v1/analysis/files/csv/{file_id}", response_model=CsvFilePreview)
    def preview(file_id: UUID, request: Request):
        port = configured()
        actor, token = credentials(request)
        try:
            return port.preview(file_id, actor, token)
        except Exception as exc:
            raise denied(exc) from exc

    @router.post("/v1/analysis/files/csv/{file_id}/confirm", response_model=CsvProfileResult)
    async def confirm(file_id: UUID, request: Request):
        port = configured()
        actor, token = credentials(request)
        if request.headers.get("content-type", "").lower() != "application/json":
            raise HTTPException(status_code=415, detail="csv_confirmation_json_required")
        try:
            body, end = await collect(request, port, actor, token, 1024)
            confirmation = ConfirmCsvText.model_validate_json(body)
            return await run_in_threadpool(
                port.confirm, file_id, actor, token, confirmation, deadline=end
            )
        except Exception as exc:
            raise denied(exc) from exc

    @router.get("/v1/analysis/files/csv/{file_id}/profile", response_model=CsvProfileResult)
    def profile(file_id: UUID, request: Request):
        port = configured()
        actor, token = credentials(request)
        try:
            return port.result(file_id, actor, token)
        except Exception as exc:
            raise denied(exc) from exc

    return router
