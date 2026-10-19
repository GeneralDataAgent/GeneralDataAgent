"""Explicitly configured Feishu notifications, exact readback and conservative recovery.

The CLI keeps credentials in its own protected store. No shell, automatic resend or
secret-bearing errors. Recall confirmation is a separate trusted human approval port.
"""

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import Field, model_validator

from generaldataagent.business.actions import content_hash
from generaldataagent.contracts.business import ActionIntent, ExternalMessageReceipt, FeishuBinding
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.dlp import TextDLPScanner


class FeishuTarget(ContractModel):
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9]{1,64}$")
    sender_id: str = Field(pattern=r"^ou_[A-Za-z0-9]{1,128}$")
    identity: Literal["bot", "user"] = "bot"
    receiver_type: Literal["chat_id", "open_id"]
    receiver_id: str = Field(pattern=r"^(oc|ou)_[A-Za-z0-9]{1,128}$")
    configuration_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def matching_receiver_type(self):
        prefix = "oc_" if self.receiver_type == "chat_id" else "ou_"
        if not self.receiver_id.startswith(prefix):
            raise ValueError("Feishu receiver ID does not match its type")
        return self


class FeishuTransport(Protocol):
    def identity(self) -> dict: ...
    def send(self, target: FeishuTarget, text: str, key: str) -> dict: ...
    def fetch(self, message_id: str, identity: str) -> dict: ...
    def recall(self, message_id: str, identity: str, *, confirmed: bool) -> None: ...


class RecallConfirmationRequired(PermissionError):
    pass


class LarkCLITransport:
    def __init__(self, *, executable: str | None = None, timeout_seconds: int = 20):
        path = executable or shutil.which("lark-cli")
        if path is None or not os.path.isabs(path) or not os.path.isfile(path):
            raise ValueError("trusted absolute lark-cli executable required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValueError("bounded Feishu CLI timeout required")
        self.executable, self.timeout = path, timeout_seconds

    def _call(self, args: list[str]) -> dict:
        try:
            result = subprocess.run(
                [self.executable, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env={
                    **os.environ,
                    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
                },
            )
            if result.returncode == 10:
                raise RecallConfirmationRequired(
                    "Feishu high-risk recall requires explicit confirmation"
                )
            if result.returncode or len(result.stdout.encode()) > 262144:
                raise ValueError("CLI failure")
            body = json.loads(result.stdout)
            if body.get("ok") is not True:
                raise ValueError("CLI envelope failure")
            return body["data"]
        except RecallConfirmationRequired:
            raise
        except Exception:
            raise PermissionError("feishu_transport_unavailable") from None

    def identity(self) -> dict:
        # auth status uses its own documented output, not the service success envelope.
        try:
            result = subprocess.run(
                [self.executable, "auth", "status", "--json", "--verify"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env={
                    **os.environ,
                    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
                },
            )
            if result.returncode or len(result.stdout.encode()) > 65536:
                raise ValueError("identity unavailable")
            body = json.loads(result.stdout)
            if body.get("verified") is not True:
                raise ValueError("identity not verified")
            return body
        except Exception:
            raise PermissionError("feishu_identity_unavailable") from None

    def send(self, target: FeishuTarget, text: str, key: str) -> dict:
        flag = "--chat-id" if target.receiver_type == "chat_id" else "--user-id"
        return self._call(
            [
                "im",
                "+messages-send",
                flag,
                target.receiver_id,
                "--as",
                target.identity,
                "--content",
                json.dumps({"text": text}, ensure_ascii=False),
                "--idempotency-key",
                key,
                "--format",
                "json",
            ]
        )

    def fetch(self, message_id: str, identity: str) -> dict:
        result = self._call(
            [
                "im",
                "+messages-mget",
                "--message-ids",
                message_id,
                "--as",
                identity,
                "--no-reactions",
                "--format",
                "json",
            ]
        )
        messages = result.get("messages", [])
        if len(messages) != 1 or messages[0].get("message_id") != message_id:
            raise PermissionError("feishu_message_unavailable")
        return messages[0]

    def recall(self, message_id: str, identity: str, *, confirmed: bool) -> None:
        if confirmed is not True:
            raise RecallConfirmationRequired(
                "Feishu high-risk recall requires explicit confirmation"
            )
        self._call(
            [
                "im",
                "messages",
                "delete",
                "--params",
                json.dumps({"message_id": message_id}),
                "--as",
                identity,
                "--format",
                "json",
                "--yes",
            ]
        )


class FeishuNotificationBackend:
    def __init__(
        self,
        *,
        target: FeishuTarget,
        transport: FeishuTransport,
        admitted: Callable[[], bool],
        recall_confirmed: Callable[[ActionIntent], bool] = lambda intent: False,
    ):
        self.target, self.transport = target, transport
        self.admitted, self.recall_confirmed = admitted, recall_confirmed

    @staticmethod
    def wire_text(intent: ActionIntent) -> str:
        if intent.payload.kind != "notification":
            raise ValueError("Feishu backend accepts notification intents only")
        action_id = intent.compensates or intent.action_id
        return (
            f"{intent.payload.message}\n\n"
            f"[DataAgent:{action_id}; evidence:{intent.payload.evidence_hash}]"
        )

    def bind(self, intent: ActionIntent) -> ActionIntent:
        text = self.wire_text(intent)
        TextDLPScanner().check(text.encode())
        binding = FeishuBinding(
            **self.target.model_dump(exclude={"schema_version", "configuration_version"}),
            configuration_hash=content_hash(self.target.model_dump(mode="json")),
            wire_content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        )
        if intent.delivery_binding is not None and intent.delivery_binding != binding:
            raise PermissionError("feishu_approved_binding_changed")
        return intent.model_copy(update={"delivery_binding": binding})

    def check(self, intent: ActionIntent) -> None:
        if self.admitted() is not True or self.bind(intent) != intent:
            raise PermissionError("feishu_backend_not_admitted_or_binding_changed")
        identity = self.transport.identity()
        actual = identity.get("identities", {}).get(self.target.identity, {})
        if (
            identity.get("appId") != self.target.app_id
            or actual.get("openId") != self.target.sender_id
            or actual.get("verified") is not True
        ):
            raise PermissionError("feishu_sender_changed")

    def _verify(
        self, intent: ActionIntent, message: dict, *, recalled: bool = False
    ) -> ExternalMessageReceipt:
        binding = intent.delivery_binding
        sender = message.get("sender", {})
        sender_id = sender.get("open_bot_id") or sender.get("id") or sender.get("open_id")
        if binding is not None and binding.identity == "bot" and sender_id == binding.app_id:
            sender_id = binding.sender_id
        chat_id = message.get("chat_id")
        if (
            binding is None
            or sender_id != binding.sender_id
            or (binding.receiver_type == "chat_id" and chat_id != binding.receiver_id)
        ):
            raise PermissionError("feishu_receiver_or_sender_mismatch")
        content = message.get("content")
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
                text = decoded.get("text") if isinstance(decoded, dict) else content
            except ValueError:
                text = content
        elif isinstance(content, dict):
            text = content.get("text")
        else:
            text = None
        if recalled:
            if message.get("deleted") is not True:
                raise PermissionError("feishu_recall_not_verified")
        elif (
            message.get("msg_type") != "text"
            or text != self.wire_text(intent)
            or message.get("deleted") is True
        ):
            raise PermissionError("feishu_delivered_content_mismatch")
        return ExternalMessageReceipt(
            message_id=message["message_id"],
            chat_id=chat_id,
            sender_id=sender_id,
            wire_content_hash=binding.wire_content_hash,
            configuration_hash=binding.configuration_hash,
            state="recalled" if recalled else "delivered",
        )

    def deliver(
        self,
        intent: ActionIntent,
        *,
        prior: ExternalMessageReceipt | None = None,
        before_write: Callable[[], None],
        record_hint: Callable[[dict], None] = lambda hint: None,
    ) -> ExternalMessageReceipt:
        self.check(intent)
        if intent.operation == "compensate":
            if (
                prior is None
                or prior.state != "delivered"
                or prior.configuration_hash != intent.delivery_binding.configuration_hash
                or prior.wire_content_hash != intent.delivery_binding.wire_content_hash
            ):
                raise PermissionError("feishu_compensation_receipt_mismatch")
            original = self.transport.fetch(prior.message_id, self.target.identity)
            verified = self._verify(intent, original)
            if verified != prior:
                raise PermissionError("feishu_original_message_changed")
            confirmed = self.recall_confirmed(intent) is True
            if not confirmed:
                raise RecallConfirmationRequired("Feishu recall awaits explicit human confirmation")
            before_write()
            self.transport.recall(prior.message_id, self.target.identity, confirmed=confirmed)
            return self._verify(
                intent, self.transport.fetch(prior.message_id, self.target.identity), recalled=True
            )
        before_write()
        response = self.transport.send(self.target, self.wire_text(intent), str(intent.action_id))
        record_hint({"message_id": response["message_id"], "chat_id": response.get("chat_id")})
        verified = self._verify(
            intent, self.transport.fetch(response["message_id"], self.target.identity)
        )
        if response.get("chat_id") != verified.chat_id:
            raise PermissionError("feishu_receiver_receipt_mismatch")
        return verified

    def reconcile(self, intent: ActionIntent, message_id: str) -> ExternalMessageReceipt:
        self.check(intent)
        return self._verify(
            intent,
            self.transport.fetch(message_id, self.target.identity),
            recalled=intent.operation == "compensate",
        )
