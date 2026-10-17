"""Fixed-endpoint HTTPS audit/paging transports; no redirects or secret-bearing errors."""

from urllib.request import Request, build_opener

from pydantic import SecretStr

from generaldataagent.observability.alerts import AlertReceipt, CriticalAlert
from generaldataagent.observability.verified_audit import AuditReceipt
from generaldataagent.security.login import _https_url, _NoRedirect


class _HTTPSAppend:
    def __init__(self, *, endpoint: str, bearer_token: SecretStr):
        _https_url(endpoint)
        if len(bearer_token.get_secret_value()) < 32:
            raise ValueError("strong service append credential required")
        self.endpoint, self.token = endpoint, bearer_token

    def post(self, content: bytes) -> bytes:
        if len(content) > 65536:
            raise ValueError("service request too large")
        request = Request(
            self.endpoint,
            data=content,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token.get_secret_value(),
            },
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=10) as response:
                result = response.read(16385)
                if len(result) > 16384:
                    raise ValueError("service response too large")
                return result
        except Exception:
            raise PermissionError("service_append_unavailable") from None


class AuditHTTPTransport(_HTTPSAppend):
    def __call__(self, content: bytes) -> AuditReceipt:
        try:
            return AuditReceipt.model_validate_json(self.post(content))
        except Exception:
            raise PermissionError("audit_append_unavailable") from None


class AlertHTTPTransport(_HTTPSAppend):
    def __call__(self, alert: CriticalAlert) -> AlertReceipt:
        try:
            return AlertReceipt.model_validate_json(self.post(alert.model_dump_json().encode()))
        except Exception:
            raise PermissionError("alert_append_unavailable") from None
