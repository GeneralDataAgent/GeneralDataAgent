"""Trusted explicitly configured business receiver port; no dynamic plugin loading."""

from collections.abc import Callable
from typing import Protocol

from generaldataagent.contracts.business import ActionIntent, ReceiverReceipt


class BusinessReceiver(Protocol):
    backend_name: str
    action_kind: str
    admitted: Callable[[], bool]

    def prepare(
        self, intent: ActionIntent, report_content: bytes | None = None
    ) -> ActionIntent: ...
    def bind(self, intent: ActionIntent) -> ActionIntent: ...
    def check(self, intent: ActionIntent) -> None: ...
    def impact(self, intent: ActionIntent) -> tuple[str, str]: ...
    def deliver(
        self,
        intent: ActionIntent,
        *,
        prior: ReceiverReceipt | None,
        before_write: Callable[[], None],
        record_hint: Callable[[dict], None],
    ) -> ReceiverReceipt: ...
    def reconcile(self, intent: ActionIntent, message_id: str) -> ReceiverReceipt: ...
