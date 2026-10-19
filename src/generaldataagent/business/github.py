"""Approved draft scheduling PRs for pinned, non-secret JSON manifests on github.com.

Only an operator-configured repository/file/key is writable. There is no merge,
force-push, workflow-file editor, branch deletion or automatic retry operation.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from time import monotonic
from typing import Protocol
from urllib.parse import quote

from pydantic import Field, field_validator

from generaldataagent.business.actions import content_hash
from generaldataagent.contracts.business import ActionIntent, GitHubPRBinding, GitHubPRReceipt
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.dlp import TextDLPScanner


class GitHubNotFound(ValueError):
    pass


class GitHubTransport(Protocol):
    def request(self, method: str, endpoint: str, body: dict | None = None) -> dict | list: ...


class GitHubCLITransport:
    def __init__(self, *, executable: str | None = None, timeout_seconds: int = 15):
        path = executable or shutil.which("gh")
        if not path or not os.path.isabs(path) or not os.path.isfile(path):
            raise ValueError("trusted absolute gh executable required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValueError("bounded GitHub transport deadline required")
        self.executable, self.timeout = path, timeout_seconds

    def request(self, method: str, endpoint: str, body: dict | None = None):
        if (
            method not in {"GET", "POST", "PUT", "PATCH"}
            or not re.fullmatch(r"(?:user|repos/[A-Za-z0-9_./%?=&-]+)", endpoint)
            or ".." in endpoint
        ):
            raise ValueError("unsupported_github_operation")
        args = [
            self.executable,
            "api",
            "--hostname",
            "github.com",
            "--include",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            endpoint,
        ]
        if body is not None:
            args += ["--input", "-"]
        try:
            response = subprocess.run(
                args,
                input=json.dumps(body, ensure_ascii=False) if body is not None else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env={
                    **{k: v for k, v in os.environ.items() if k != "GH_DEBUG"},
                    "GH_PROMPT_DISABLED": "1",
                    "GH_PAGER": "cat",
                },
            )
            if len(response.stdout.encode()) > 524288:
                raise ValueError("response too large")
            parts = re.split(r"\r?\n\r?\n", response.stdout, maxsplit=1)
            status = re.match(r"HTTP/\S+ (\d{3})\b", parts[0])
            if status and status[1] == "404":
                raise GitHubNotFound("github_resource_not_found")
            if (
                response.returncode
                or len(parts) != 2
                or not status
                or not 200 <= int(status[1]) < 300
            ):
                raise ValueError("request failed")
            result = json.loads(parts[1])
            if not isinstance(result, (dict, list)):
                raise ValueError("invalid response")
            return result
        except GitHubNotFound:
            raise
        except Exception:
            raise PermissionError("github_transport_unavailable") from None


class GitHubScheduleTarget(ContractModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
    repository_id: int = Field(gt=0)
    actor_id: int = Field(gt=0)
    base_branch: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,99}$")
    path: str = Field(pattern=r"^[A-Za-z0-9_./-]{1,200}\.json$")
    pipeline: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    interval_path: tuple[str, ...] = Field(min_length=1, max_length=8)
    configuration_version: str = Field(min_length=1, max_length=128)
    max_operation_seconds: int = Field(default=90, ge=1, le=120)

    @field_validator("repository", "base_branch", "path")
    @classmethod
    def safe_git_paths(cls, value):
        if (
            ".." in value
            or "//" in value
            or value.endswith((".", "/", ".lock"))
            or any(p.startswith(".") for p in value.split("/"))
        ):
            raise ValueError("unsupported_git_path")
        if len(value.split("/")) > 8:
            raise ValueError("git_path_too_deep")
        return value

    @field_validator("interval_path")
    @classmethod
    def safe_json_keys(cls, keys):
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key) for key in keys):
            raise ValueError("explicit_json_object_keys_required")
        return keys


def blob_sha(content: str) -> str:
    raw = content.encode()
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def strict_document(text: str):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("nonfinite_json_value")

    if len(text.encode()) > 8192:
        raise ValueError("schedule_manifest_too_large")
    TextDLPScanner().check(text.encode())
    result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(result, dict):
        raise ValueError("schedule_manifest_must_be_an_object")
    return result


class GitHubSchedulePRBackend:
    backend_name = "github"
    action_kind = "schedule_pr"

    def __init__(
        self,
        *,
        target: GitHubScheduleTarget,
        transport: GitHubTransport,
        admitted: Callable[[], bool],
    ):
        self.target, self.transport, self.admitted = target, transport, admitted
        self._deadline: ContextVar[float | None] = ContextVar(
            "github_operation_deadline", default=None
        )

    @property
    def root(self):
        return f"repos/{self.target.repository}"

    @contextmanager
    def _bounded(self):
        previous = self._deadline.get()
        token = self._deadline.set(previous or monotonic() + self.target.max_operation_seconds)
        try:
            yield
        finally:
            self._deadline.reset(token)

    def _request(self, method, endpoint, body=None):
        deadline = self._deadline.get()
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("github_operation_deadline")
        result = self.transport.request(method, endpoint, body)
        if not isinstance(result, dict):
            raise PermissionError("github_response_must_be_an_object")
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("github_operation_deadline")
        return result

    def _authorize(self):
        if self.admitted() is not True:
            raise PermissionError("github_backend_not_admitted")
        user = self._request("GET", "user")
        repo = self._request("GET", self.root)
        if (
            user.get("id") != self.target.actor_id
            or repo.get("id") != self.target.repository_id
            or repo.get("full_name", "").casefold() != self.target.repository.casefold()
            or repo.get("archived") is not False
        ):
            raise PermissionError("github_actor_or_repository_changed")

    @staticmethod
    def _sha(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise PermissionError("invalid_github_object_sha")
        return value

    def _head(self, branch):
        ref = self._request("GET", self.root + "/git/ref/heads/" + quote(branch, safe=""))
        if (
            ref.get("ref") != "refs/heads/" + branch
            or ref.get("object", {}).get("type") != "commit"
        ):
            raise PermissionError("github_branch_mismatch")
        return self._sha(ref["object"]["sha"])

    def _file(self, commit_sha):
        commit = self._request("GET", self.root + "/git/commits/" + commit_sha)
        if commit.get("sha") != commit_sha:
            raise PermissionError("github_commit_mismatch")
        tree_sha = self._sha(commit["tree"]["sha"])
        parts = self.target.path.split("/")
        for index, name in enumerate(parts):
            tree = self._request("GET", self.root + "/git/trees/" + tree_sha)
            if tree.get("sha") != tree_sha or tree.get("truncated") is not False:
                raise PermissionError("github_tree_unavailable")
            entries = [entry for entry in tree["tree"] if entry["path"] == name]
            if len(entries) != 1:
                raise PermissionError("github_manifest_missing_or_ambiguous")
            entry = entries[0]
            if index < len(parts) - 1:
                if entry.get("type") != "tree" or entry.get("mode") != "040000":
                    raise PermissionError("github_manifest_directory_invalid")
                tree_sha = self._sha(entry["sha"])
            elif entry.get("type") != "blob" or entry.get("mode") != "100644":
                raise PermissionError("github_manifest_requires_regular_file")
        sha = self._sha(entry["sha"])
        blob = self._request("GET", self.root + "/git/blobs/" + sha)
        if (
            blob.get("sha") != sha
            or blob.get("encoding") != "base64"
            or type(blob.get("size")) is not int
            or not 0 < blob["size"] <= 8192
        ):
            raise PermissionError("github_manifest_blob_invalid")
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        text = raw.decode("utf-8")
        if len(raw) != blob["size"] or blob_sha(text) != sha:
            raise PermissionError("github_manifest_blob_changed")
        return text, sha

    def _plan(self, intent, base, old, old_sha):
        payload = intent.payload
        if (
            payload.kind != "schedule_pr"
            or payload.pipeline != self.target.pipeline
            or blob_sha(old) != old_sha
        ):
            raise PermissionError("github_pipeline_or_source_mismatch")
        document = deepcopy(strict_document(old))
        node = document
        for key in self.target.interval_path[:-1]:
            if key not in node:
                raise ValueError("schedule_interval_path_missing")
            node = node[key]
            if not isinstance(node, dict):
                raise ValueError("schedule_interval_path_not_an_object")
        key = self.target.interval_path[-1]
        if (
            type(node.get(key)) is not int
            or node[key] != payload.rollback_interval_minutes
            or node[key] == payload.interval_minutes
        ):
            raise ValueError("schedule_baseline_changed_or_noop")
        node[key] = payload.interval_minutes
        new = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        strict_document(new)
        origin = intent.compensates or intent.action_id
        summary = json.dumps(
            {
                "action_id": str(origin),
                "pipeline": payload.pipeline,
                "path": self.target.path,
                "base_sha": base,
                "from_minutes": payload.rollback_interval_minutes,
                "to_minutes": payload.interval_minutes,
                "rationale": payload.rationale,
            },
            ensure_ascii=False,
            indent=2,
        )
        fence = "`" * (max([2, *[len(s) for s in re.findall(r"`+", summary)]]) + 1)
        return GitHubPRBinding(
            configuration_hash=content_hash(self.target.model_dump(mode="json")),
            repository=self.target.repository,
            repository_id=self.target.repository_id,
            actor_id=self.target.actor_id,
            base_branch=self.target.base_branch,
            base_sha=base,
            branch=f"dataagent/schedule-{origin}",
            path=self.target.path,
            old_content=old,
            new_content=new,
            old_blob_sha=old_sha,
            new_blob_sha=blob_sha(new),
            title=(
                f"Schedule {payload.pipeline}: {payload.rollback_interval_minutes} -> "
                f"{payload.interval_minutes} minutes"
            ),
            body=(
                "GeneralDataAgent scheduling proposal. Merge requires separate human review.\n\n"
                f"{fence}json\n{summary}\n{fence}"
            ),
        )

    def prepare(self, intent: ActionIntent, report_content: bytes | None = None):
        if (
            intent.payload.kind != self.action_kind
            or intent.payload.pipeline != self.target.pipeline
        ):
            raise PermissionError("github_pipeline_mismatch")
        if intent.delivery_binding is not None:
            return self.bind(intent)
        if intent.operation != "deliver":
            raise ValueError("github_compensation_requires_original_plan")
        with self._bounded():
            self._authorize()
            base = self._head(self.target.base_branch)
            old, sha = self._file(base)
            binding = self._plan(intent, base, old, sha)
            TextDLPScanner().check(binding.model_dump_json().encode())
            return intent.model_copy(update={"delivery_binding": binding})

    def bind(self, intent):
        binding = intent.delivery_binding
        if (
            not isinstance(binding, GitHubPRBinding)
            or self._plan(intent, binding.base_sha, binding.old_content, binding.old_blob_sha)
            != binding
        ):
            raise PermissionError("github_approved_binding_changed")
        return intent

    def check(self, intent):
        self.bind(intent)
        with self._bounded():
            self._authorize()

    def impact(self, intent):
        b = intent.delivery_binding
        if intent.operation == "compensate":
            return (
                f"Close only the original unmerged draft PR at {b.repository}, branch {b.branch}, "
                f"from action {intent.compensates}; leave branches and merged changes intact.",
                "Bounded identity/readback calls and closure of the exact original "
                "unmerged draft PR",
            )
        return (
            f"GitHub {intent.operation}: {b.repository}/{b.path}, "
            f"base {b.base_sha}, branch {b.branch}. "
            "Create branch/commit and draft PR; repository automation may run. No automatic merge.",
            "Bounded GitHub reads and branch/commit/PR writes, or closure of the exact unmerged PR",
        )

    def _verify(self, intent, number, *, recalled=False):
        b = intent.delivery_binding
        pr = self._request("GET", self.root + f"/pulls/{number}")
        if (
            pr.get("number") != number
            or pr.get("user", {}).get("id") != b.actor_id
            or pr.get("base", {}).get("repo", {}).get("id") != b.repository_id
            or pr.get("head", {}).get("repo", {}).get("id") != b.repository_id
            or pr.get("base", {}).get("ref") != b.base_branch
            or pr["base"].get("sha") != b.base_sha
            or pr["head"].get("ref") != b.branch
            or pr.get("title") != b.title
            or pr.get("body") != b.body
            or pr.get("draft") is not True
            or pr.get("merged") is not False
            or pr.get("auto_merge") is not None
            or pr.get("state") != ("closed" if recalled else "open")
        ):
            raise PermissionError("github_pr_readback_mismatch")
        head = self._sha(pr["head"]["sha"])
        if self._head(b.branch) != head:
            raise PermissionError("github_pr_head_changed")
        self._verify_commit(b, head)
        return GitHubPRReceipt(
            repository=b.repository,
            repository_id=b.repository_id,
            actor_id=b.actor_id,
            pull_number=number,
            head_sha=head,
            binding_hash=content_hash(b.model_dump(mode="json")),
            configuration_hash=b.configuration_hash,
            state="recalled" if recalled else "delivered",
        )

    def _verify_commit(self, b, head):
        commit = self._request("GET", self.root + f"/commits/{head}?per_page=100")
        files = commit.get("files", [])
        if (
            commit.get("sha") != head
            or [p["sha"] for p in commit.get("parents", [])] != [b.base_sha]
            or len(files) != 1
            or files[0].get("filename") != b.path
            or files[0].get("status") != "modified"
            or files[0].get("sha") != b.new_blob_sha
        ):
            raise PermissionError("github_pr_changed_files_mismatch")
        content, sha = self._file(head)
        if content != b.new_content or sha != b.new_blob_sha:
            raise PermissionError("github_pr_content_mismatch")

    def deliver(self, intent, *, prior=None, before_write, record_hint=lambda hint: None):
        with self._bounded():
            self.check(intent)
            b = intent.delivery_binding

            def guard():
                self.check(intent)
                before_write()
                if self._deadline.get() <= monotonic():
                    raise TimeoutError("github_operation_deadline")

            if intent.operation == "compensate":
                if (
                    not isinstance(prior, GitHubPRReceipt)
                    or self._verify(intent, prior.pull_number) != prior
                ):
                    raise PermissionError("github_compensation_receipt_mismatch")
                guard()
                self._request(
                    "PATCH", self.root + f"/pulls/{prior.pull_number}", {"state": "closed"}
                )
                return self._verify(intent, prior.pull_number, recalled=True)
            if self._head(b.base_branch) != b.base_sha:
                raise PermissionError("github_base_branch_changed")
            guard()
            self._request(
                "POST",
                self.root + "/git/refs",
                {"ref": "refs/heads/" + b.branch, "sha": b.base_sha},
            )
            record_hint({"backend": "github", "stage": "branch_created", "branch": b.branch})
            if self._head(b.branch) != b.base_sha or self._head(b.base_branch) != b.base_sha:
                raise PermissionError("github_branch_changed_before_commit")
            guard()
            result = self._request(
                "PUT",
                self.root + "/contents/" + quote(b.path, safe="/"),
                {
                    "message": f"GeneralDataAgent schedule action {intent.action_id}",
                    "branch": b.branch,
                    "sha": b.old_blob_sha,
                    "content": base64.b64encode(b.new_content.encode()).decode(),
                },
            )
            head = self._sha(result["commit"]["sha"])
            record_hint(
                {
                    "backend": "github",
                    "stage": "commit_created",
                    "branch": b.branch,
                    "head_sha": head,
                }
            )
            if result.get("content", {}).get("sha") != b.new_blob_sha:
                raise PermissionError("github_commit_content_mismatch")
            self._verify_commit(b, head)
            if self._head(b.branch) != head or self._head(b.base_branch) != b.base_sha:
                raise PermissionError("github_branch_changed_before_pr")
            guard()
            result = self._request(
                "POST",
                self.root + "/pulls",
                {
                    "title": b.title,
                    "body": b.body,
                    "head": b.branch,
                    "base": b.base_branch,
                    "draft": True,
                    "maintainer_can_modify": False,
                },
            )
            number = result.get("number")
            if type(number) is not int or number <= 0:
                raise PermissionError("github_pr_identifier_invalid")
            record_hint(
                {
                    "backend": "github",
                    "stage": "pr_created",
                    "branch": b.branch,
                    "head_sha": head,
                    "pull_number": number,
                }
            )
            verified = self._verify(intent, number)
            if verified.head_sha != head:
                raise PermissionError("github_created_head_changed")
            return verified

    def reconcile(self, intent, message_id):
        if not re.fullmatch(r"[1-9][0-9]{0,9}", message_id):
            raise ValueError("invalid_github_pull_number")
        with self._bounded():
            self.check(intent)
            return self._verify(intent, int(message_id), recalled=intent.operation == "compensate")
