"""GitHub review queues for tickets, dashboard drafts and model registration requests.

These are review artifacts, never a BI query executor or a model deployment port.
"""

import json
import re
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Literal

from pydantic import Field, field_validator

from generaldataagent.business.actions import content_hash
from generaldataagent.business.github import GitHubTransport
from generaldataagent.contracts.business import (
    ActionIntent,
    GitHubReviewBinding,
    GitHubReviewReceipt,
)
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.dlp import TextDLPScanner


class GitHubReviewTarget(ContractModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
    repository_id: int = Field(gt=0, strict=True)
    actor_id: int = Field(gt=0, strict=True)
    kind: Literal["ticket", "dashboard", "model_registration"]
    configuration_version: str = Field(min_length=1, max_length=128)
    max_operation_seconds: int = Field(default=60, ge=1, le=120, strict=True)

    @field_validator("repository")
    @classmethod
    def safe_repository(cls, value):
        if ".." in value or any(p.startswith(".") or p.endswith(".") for p in value.split("/")):
            raise ValueError("unsupported_repository")
        return value


class GitHubReviewBackend:
    backend_name = "github_review"

    def __init__(
        self,
        *,
        target: GitHubReviewTarget,
        transport: GitHubTransport,
        admitted: Callable[[], bool],
    ):
        self.target, self.transport, self.admitted = target, transport, admitted
        self.action_kind = target.kind
        self._deadline: ContextVar[float | None] = ContextVar(
            "github_review_deadline", default=None
        )

    @property
    def root(self):
        return f"repos/{self.target.repository}"

    @contextmanager
    def bounded(self):
        token = self._deadline.set(
            self._deadline.get() or monotonic() + self.target.max_operation_seconds
        )
        try:
            yield
        finally:
            self._deadline.reset(token)

    def request(self, method, endpoint, body=None):
        if monotonic() >= self._deadline.get():
            raise TimeoutError("github_review_deadline")
        result = self.transport.request(method, endpoint, body)
        if monotonic() >= self._deadline.get():
            raise TimeoutError("github_review_deadline")
        if not isinstance(result, dict):
            raise PermissionError("github_review_response_invalid")
        return result

    def authorize(self):
        if self.admitted() is not True:
            raise PermissionError("github_review_not_admitted")
        user = self.request("GET", "user")
        repo = self.request("GET", self.root)
        if (
            type(user.get("id")) is not int
            or user["id"] != self.target.actor_id
            or type(repo.get("id")) is not int
            or repo["id"] != self.target.repository_id
            or repo.get("full_name", "").casefold() != self.target.repository.casefold()
            or repo.get("archived") is not False
            or repo.get("has_issues") is not True
        ):
            raise PermissionError("github_review_identity_or_repository_changed")

    def plan(self, intent):
        payload = intent.payload
        if payload.kind != self.action_kind:
            raise PermissionError("github_review_kind_mismatch")
        title = getattr(
            payload, "title", f"Model registration request: {getattr(payload, 'experiment_id', '')}"
        )
        # Titles are not fenced, so disallow mention syntax and control characters there.
        if "@" in title or any(ord(c) < 32 for c in title):
            raise ValueError("review_title_contains_mentions_or_controls")
        origin = intent.compensates or intent.action_id
        document = {
            "action_id": str(origin),
            "kind": self.action_kind,
            "status": "pending_human_review",
            "deployment_allowed": False,
            "payload": payload.model_dump(mode="json"),
        }
        if payload.kind == "dashboard":
            document["dashboard_draft"] = {
                "title": payload.title,
                "panels": [
                    {"title": name, "query_enabled": False} for name in payload.metric_names
                ],
                "evidence_hashes": list(payload.evidence_hashes),
            }
        summary = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        fence = "`" * (max([2, *[len(v) for v in re.findall(r"`+", summary)]]) + 1)
        body = (
            "GeneralDataAgent review request. Human review is required; no query, registration "
            "or deployment is performed.\n\n" + f"{fence}json\n{summary}\n{fence}"
        )
        TextDLPScanner().check(body.encode())
        return GitHubReviewBinding(
            repository=self.target.repository,
            repository_id=self.target.repository_id,
            actor_id=self.target.actor_id,
            kind=self.action_kind,
            title=title,
            body=body,
            configuration_hash=content_hash(self.target.model_dump(mode="json")),
        )

    def prepare(self, intent, report_content=None):
        if intent.delivery_binding is not None:
            return self.bind(intent)
        with self.bounded():
            self.authorize()
            return intent.model_copy(update={"delivery_binding": self.plan(intent)})

    def bind(self, intent):
        if intent.delivery_binding != self.plan(intent):
            raise PermissionError("github_review_binding_changed")
        return intent

    def check(self, intent):
        self.bind(intent)
        with self.bounded():
            self.authorize()

    def impact(self, intent):
        return (
            f"GitHub {intent.operation} {self.action_kind} review at "
            f"{self.target.repository}; "
            "repository notifications/automation may run. "
            "Compensation closes the unchanged request only.",
            "Bounded identity/readback and one issue creation or closure; no model or BI execution",
        )

    def verify(self, intent, number, *, recalled=False):
        binding = intent.delivery_binding
        issue = self.request("GET", self.root + f"/issues/{number}")
        if (
            type(issue.get("number")) is not int
            or issue["number"] != number
            or type(issue.get("id")) is not int
            or issue["id"] <= 0
            or issue.get("url") != f"https://api.github.com/{self.root}/issues/{number}"
            or issue.get("user", {}).get("id") != binding.actor_id
            or "pull_request" in issue
            or issue.get("title") != binding.title
            or issue.get("body") != binding.body
            or issue.get("state") != ("closed" if recalled else "open")
            or issue.get("locked") is not False
            or issue.get("assignees") != []
            or issue.get("labels") != []
            or issue.get("milestone") is not None
        ):
            raise PermissionError("github_review_readback_mismatch")
        return GitHubReviewReceipt(
            repository=binding.repository,
            repository_id=binding.repository_id,
            actor_id=binding.actor_id,
            issue_number=number,
            issue_id=issue["id"],
            binding_hash=content_hash(binding.model_dump(mode="json")),
            configuration_hash=binding.configuration_hash,
            state="recalled" if recalled else "delivered",
        )

    def deliver(self, intent, *, prior=None, before_write, record_hint=lambda hint: None):
        with self.bounded():
            self.check(intent)
            if intent.operation == "compensate":
                if (
                    not isinstance(prior, GitHubReviewReceipt)
                    or self.verify(intent, prior.issue_number) != prior
                ):
                    raise PermissionError("github_review_compensation_mismatch")
                self.check(intent)
                before_write()
                self.request(
                    "PATCH",
                    self.root + f"/issues/{prior.issue_number}",
                    {"state": "closed", "state_reason": "not_planned"},
                )
                return self.verify(intent, prior.issue_number, recalled=True)
            self.check(intent)
            before_write()
            b = intent.delivery_binding
            issue = self.request("POST", self.root + "/issues", {"title": b.title, "body": b.body})
            number = issue.get("number")
            if type(number) is not int or not 0 < number <= 9999999999:
                raise PermissionError("github_review_number_invalid")
            record_hint({"backend": "github_review", "issue_number": number})
            return self.verify(intent, number)

    def reconcile(self, intent: ActionIntent, message_id: str):
        if not re.fullmatch(r"[1-9][0-9]{0,9}", message_id):
            raise ValueError("github_review_number_invalid")
        with self.bounded():
            self.check(intent)
            return self.verify(intent, int(message_id), recalled=intent.operation == "compensate")
