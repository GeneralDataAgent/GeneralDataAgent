"""Common connector interface and normalized failure model."""

from enum import StrEnum
from threading import Event
from typing import Any, Protocol

from generaldataagent.contracts.data_engineering import (
    ConnectorCapabilities,
    RelationSchema,
)


class ConnectorErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    TEMPORARY_UNAVAILABLE = "temporary_unavailable"
    EXECUTION_FAILED = "execution_failed"
    CANCELLED = "cancelled"


class ConnectorError(RuntimeError):
    def __init__(
        self,
        code: ConnectorErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ReadOnlyConnector(Protocol):
    @property
    def capabilities(self) -> ConnectorCapabilities: ...

    def inspect_relation(
        self,
        relation: str,
        *,
        cancel_event: Event | None = None,
    ) -> RelationSchema: ...

    def fetch_all(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
        *,
        cancel_event: Event | None = None,
    ) -> tuple[tuple[Any, ...], ...]: ...


def quote_identifier(identifier: str) -> str:
    """Quote one already-discovered SQL identifier without accepting paths."""

    if not identifier or "\x00" in identifier:
        raise ConnectorError(
            ConnectorErrorCode.INVALID_REQUEST,
            "SQL identifiers must be non-empty and cannot contain NUL bytes",
        )
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'
