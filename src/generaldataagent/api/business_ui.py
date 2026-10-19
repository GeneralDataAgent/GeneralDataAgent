"""Credential-free browser shell for the scoped business pilot; grants remain external."""

import json
from importlib.resources import files

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from generaldataagent.business.actions import BusinessActionRuntime

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def business_ui_router(actions: BusinessActionRuntime) -> APIRouter:
    router = APIRouter(prefix="/business", tags=["Business console"])

    def asset(name, media_type):
        return Response(
            files("generaldataagent.api").joinpath("web", name).read_bytes(),
            media_type=media_type,
            headers=HEADERS,
        )

    @router.get("")
    def page():
        return asset("business.html", "text/html")

    @router.get("/console.js")
    def script():
        return asset("business.js", "text/javascript")

    @router.get("/console.css")
    def styles():
        return asset("business.css", "text/css")

    @router.get("/session")
    def session(request: Request):
        try:
            if actions.service_enabled() is not True:
                raise HTTPException(status_code=503, detail="service_disabled")
            scheme, token = request.headers.get("authorization", "").split(" ", 1)
            if scheme.lower() != "bearer" or not token:
                raise ValueError("missing bearer token")
            identity = actions.broker.authenticate(token)
            if (
                "business:read" not in identity.permissions
                or identity.business_constraint is not None
            ):
                raise PermissionError("console_access_denied")
        except (ValueError, PermissionError):
            raise HTTPException(status_code=401, detail="invalid_console_session") from None
        return Response(
            content=json.dumps(
                {
                    "user_id": identity.user_id,
                    "tenant_id": identity.tenant_id,
                    "project_id": identity.project_id,
                    "data_domains": sorted(identity.data_domains),
                    "permissions": sorted(identity.permissions),
                }
            ),
            media_type="application/json",
            headers=HEADERS,
        )

    return router
