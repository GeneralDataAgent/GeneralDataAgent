import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import unquote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_stage5_actions import PAYLOADS, intent

from generaldataagent.api.app import create_app
from generaldataagent.business.actions import ActionControl, BusinessActionRuntime
from generaldataagent.business.github import (
    GitHubCLITransport,
    GitHubNotFound,
    GitHubSchedulePRBackend,
    GitHubScheduleTarget,
    blob_sha,
)
from generaldataagent.contracts.business import GitHubPRBinding
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SessionBroker

BASE, HEAD = "a" * 40, "c" * 40
ROOT = "repos/acme/jobs"


class FakeGitHub:
    def __init__(self):
        self.calls, self.prs = [], {}
        self.user_id, self.repo_id, self.repo_name = 11, 22, "acme/jobs"
        self.refs = {"main": BASE}
        self.old = json.dumps(
            {"pipelines": {"daily": {"interval_minutes": 1440, "owner": "team"}}, "enabled": True}
        )
        self.blobs = {blob_sha(self.old): self.old}
        self.file_sha = {BASE: blob_sha(self.old)}
        self.fail_after, self.extra_file = None, False
        self.mode = "100644"
        self.before_write = None

    def request(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, deepcopy(body)))
        if method == "GET":
            if endpoint == "user":
                return {"id": self.user_id}
            if endpoint == ROOT:
                return {"id": self.repo_id, "full_name": self.repo_name, "archived": False}
            if "/git/ref/heads/" in endpoint:
                branch = unquote(endpoint.split("/git/ref/heads/")[1])
                if branch not in self.refs:
                    raise GitHubNotFound("not found")
                return {
                    "ref": "refs/heads/" + branch,
                    "object": {"type": "commit", "sha": self.refs[branch]},
                }
            if "/git/commits/" in endpoint:
                sha = endpoint.rsplit("/", 1)[1]
                return {"sha": sha, "tree": {"sha": "b" * 40 if sha == BASE else "d" * 40}}
            if "/git/trees/" in endpoint:
                sha = endpoint.rsplit("/", 1)[1]
                return {
                    "sha": sha,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "schedule.json",
                            "type": "blob",
                            "mode": self.mode,
                            "sha": self.file_sha[BASE if sha == "b" * 40 else HEAD],
                        }
                    ],
                }
            if "/git/blobs/" in endpoint:
                sha = endpoint.rsplit("/", 1)[1]
                text = self.blobs[sha]
                return {
                    "sha": sha,
                    "size": len(text.encode()),
                    "encoding": "base64",
                    "content": base64.b64encode(text.encode()).decode(),
                }
            if "/commits/" in endpoint:
                files = [
                    {"filename": "schedule.json", "status": "modified", "sha": self.file_sha[HEAD]}
                ]
                if self.extra_file:
                    files.append({"filename": "unapproved.json"})
                return {"sha": HEAD, "parents": [{"sha": BASE}], "files": files}
            if "/pulls/" in endpoint:
                return deepcopy(self.prs[int(endpoint.rsplit("/", 1)[1])])
            raise AssertionError(endpoint)
        if self.before_write:
            self.before_write(method, endpoint)
        if method == "POST" and endpoint.endswith("/git/refs"):
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                raise ValueError("reference exists")
            self.refs[branch] = body["sha"]
            result = {"ref": body["ref"], "object": {"type": "commit", "sha": body["sha"]}}
            stage = "branch"
        elif method == "PUT":
            assert endpoint == ROOT + "/contents/schedule.json"
            assert body["sha"] == blob_sha(self.old)
            assert self.refs[body["branch"]] == BASE
            new = base64.b64decode(body["content"]).decode()
            sha = blob_sha(new)
            self.blobs[sha], self.file_sha[HEAD], self.refs[body["branch"]] = new, sha, HEAD
            result, stage = {"commit": {"sha": HEAD}, "content": {"sha": sha}}, "commit"
        elif method == "POST" and endpoint.endswith("/pulls"):
            assert body["draft"] is True and body["maintainer_can_modify"] is False
            self.prs[1] = {
                "number": 1,
                "user": {"id": self.user_id},
                "title": body["title"],
                "body": body["body"],
                "base": {
                    "ref": body["base"],
                    "sha": self.refs[body["base"]],
                    "repo": {"id": self.repo_id},
                },
                "head": {
                    "ref": body["head"],
                    "sha": self.refs[body["head"]],
                    "repo": {"id": self.repo_id},
                },
                "draft": True,
                "merged": False,
                "auto_merge": None,
                "state": "open",
            }
            result, stage = deepcopy(self.prs[1]), "pr"
        elif method == "PATCH":
            assert body == {"state": "closed"}
            self.prs[1]["state"] = "closed"
            result, stage = deepcopy(self.prs[1]), "close"
        else:
            raise AssertionError((method, endpoint, body))
        if self.fail_after == stage:
            raise TimeoutError("simulated lost acknowledgement")
        return result

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


@pytest.fixture
def connected(tmp_path):
    fake = FakeGitHub()
    target = GitHubScheduleTarget(
        repository="acme/jobs",
        repository_id=22,
        actor_id=11,
        base_branch="main",
        path="schedule.json",
        pipeline="daily",
        interval_path=("pipelines", "daily", "interval_minutes"),
        configuration_version="v1",
    )
    backend = GitHubSchedulePRBackend(target=target, transport=fake, admitted=lambda: True)
    broker, approvals = SessionBroker(), SQLiteApprovalStore(tmp_path / "approvals")
    runtime = BusinessActionRuntime(
        tmp_path / "actions",
        broker=broker,
        approvals=approvals,
        controls=(
            ActionControl(
                "schedule_pr",
                "schedule_pr",
                "operator",
                "t",
                "p",
                frozenset({"sales"}),
                enabled=True,
            ),
        ),
        external_backends={("schedule_pr", "schedule_pr"): backend},
    )
    token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    return runtime, approvals, fake, backend, token


def propose(connected):
    runtime, approvals, _, _, token = connected
    action = intent(PAYLOADS[2])
    view = runtime.propose(action, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    return action, view


def test_exact_draft_pr_and_one_file_change_with_persistent_replay(connected):
    runtime, approvals, fake, backend, token = connected
    action, view = propose(connected)
    binding = GitHubPRBinding.model_validate(view["intent"]["delivery_binding"])
    assert fake.writes == []
    expected = json.loads(fake.old)
    expected["pipelines"]["daily"]["interval_minutes"] = 60
    assert json.loads(binding.new_content) == expected
    result = runtime.execute(action.action_id, token)
    assert result["receipt"]["backend"] == "github"
    assert result["receipt"]["external"]["pull_number"] == 1
    assert result["receipt"]["external_delivery"] is True
    assert fake.refs["main"] == BASE
    assert [method for method, *_ in fake.writes] == ["POST", "PUT", "POST"]
    restarted = BusinessActionRuntime(
        runtime.path,
        broker=runtime.broker,
        approvals=approvals,
        controls=tuple(runtime.controls.values()),
        external_backends=runtime.external_backends,
    )
    assert restarted.execute(action.action_id, token) == result
    assert runtime.propose(action, token)["approval_id"] == view["approval_id"]
    assert len(fake.writes) == 3 and runtime.verify_audit()


def test_unapproved_or_changed_configuration_never_writes(connected):
    runtime, _, fake, backend, token = connected
    action = intent(PAYLOADS[2])
    runtime.propose(action, token)
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    backend.target = backend.target.model_copy(update={"configuration_version": "v2"})
    with pytest.raises(PermissionError, match="binding"):
        runtime.execute(action.action_id, token)
    assert fake.writes == []


@pytest.mark.parametrize("failure", ["actor", "repo", "admission", "base"])
def test_current_identity_and_source_rechecked_before_writes(connected, failure):
    runtime, _, fake, backend, token = connected
    action, _ = propose(connected)
    if failure == "actor":
        fake.user_id = 99
    elif failure == "repo":
        fake.repo_id = 99
    elif failure == "admission":
        backend.admitted = lambda: False
    else:
        fake.refs["main"] = "f" * 40
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    assert fake.writes == []


@pytest.mark.parametrize("stage", ["branch", "commit", "pr"])
def test_partial_write_quarantine_never_retries(connected, stage):
    runtime, _, fake, backend, token = connected
    action, _ = propose(connected)
    fake.fail_after = stage
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    count = len(fake.writes)
    with pytest.raises(ValueError, match="reconciliation"):
        runtime.execute(action.action_id, token)
    assert len(fake.writes) == count
    if stage == "pr":
        result = runtime.reconcile(action.action_id, token, "1")
        assert result["state"] == "completed"
        assert runtime.reconcile(action.action_id, token, "1") == result
    else:
        assert runtime.get(action.action_id, token)["state"] == "uncertain"
        assert fake.prs == {}
    assert len(fake.writes) == count and runtime.verify_audit()


def test_compensation_closes_only_original_unmerged_draft(connected):
    runtime, approvals, fake, backend, token = connected
    action, _ = propose(connected)
    runtime.execute(action.action_id, token)
    undo = intent(
        action.payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = runtime.propose(undo, token)
    with pytest.raises(PermissionError):
        runtime.execute(undo.action_id, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    result = runtime.execute(undo.action_id, token)
    assert result["receipt"]["external"]["state"] == "recalled"
    assert fake.prs[1]["state"] == "closed"
    assert len(fake.writes) == 4
    assert fake.refs["main"] == BASE
    assert not any("/merge" in endpoint or method == "DELETE" for method, endpoint, _ in fake.calls)


@pytest.mark.parametrize("changed", ["merged", "head", "body", "files", "ready", "auto_merge"])
def test_changed_pr_cannot_be_closed_or_accepted(connected, changed):
    runtime, approvals, fake, _, token = connected
    action, _ = propose(connected)
    runtime.execute(action.action_id, token)
    if changed == "merged":
        fake.prs[1]["merged"] = True
    elif changed == "head":
        fake.prs[1]["head"]["sha"] = "f" * 40
    elif changed == "body":
        fake.prs[1]["body"] = "unapproved content"
    elif changed == "files":
        fake.extra_file = True
    elif changed == "ready":
        fake.prs[1]["draft"] = False
    else:
        fake.prs[1]["auto_merge"] = {"enabled_by": "someone"}
    undo = intent(
        action.payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = runtime.propose(undo, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    with pytest.raises(PermissionError):
        runtime.execute(undo.action_id, token)
    assert len(fake.writes) == 3


def test_api_github_reconciliation_uses_typed_pull_identifier(connected):
    runtime, approvals, fake, _, token = connected
    app = TestClient(create_app(session_broker=runtime.broker, business_actions=runtime))
    headers = {
        "authorization": "Bearer " + token,
        "x-user-id": "alice",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "sales",
    }
    action, _ = propose(connected)
    fake.fail_after = "pr"
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    endpoint = f"/v1/business/drafts/{action.action_id}/reconcile"
    assert app.post(endpoint, headers=headers, json={"pull_number": 1}).status_code == 200
    assert (
        app.post(
            endpoint, headers=headers, json={"pull_number": 1, "message_id": "om_fake"}
        ).status_code
        == 422
    )
    assert app.get("/v1/capabilities", headers=headers).json()["external_business_delivery"] is True


@pytest.mark.parametrize(
    "source",
    [
        '{"x":1,"x":2}',
        '{"secret":"do-not-export-this"}',
        '{"pipelines":{"daily":{"interval_minutes":true}}}',
        '{"pipelines":{"daily":{"interval_minutes":60}}}',
    ],
)
def test_invalid_or_sensitive_source_rejected_before_persistence(connected, source):
    runtime, _, fake, _, token = connected
    fake.old = source
    fake.blobs = {blob_sha(source): source}
    fake.file_sha[BASE] = blob_sha(source)
    with pytest.raises((ValueError, PermissionError, KeyError)):
        runtime.propose(intent(PAYLOADS[2]), token)
    assert fake.writes == []
    with runtime._db() as db:
        assert db.execute("SELECT count(*) FROM business_actions").fetchone()[0] == 0


def test_cli_stdin_exact_host_status_and_sanitized_errors(monkeypatch):
    transport = GitHubCLITransport(executable="/bin/echo")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n{"number":1}',
        )

    monkeypatch.setattr("generaldataagent.business.github.subprocess.run", run)
    text = "literal $(touch /tmp/never) `echo never`"
    assert transport.request("POST", ROOT + "/pulls", {"body": text}) == {"number": 1}
    args, kwargs = calls[0]
    assert "github.com" in args and "--input" in args and text not in args
    assert json.loads(kwargs["input"]) == {"body": text}
    assert "shell" not in kwargs and kwargs["timeout"] <= 30
    monkeypatch.setattr(
        "generaldataagent.business.github.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="HTTP/2.0 404 Not Found\n\n{}"),
    )
    with pytest.raises(GitHubNotFound):
        transport.request("GET", ROOT + "/pulls/1")
    monkeypatch.setattr(
        "generaldataagent.business.github.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private credential")),
    )
    with pytest.raises(PermissionError, match="^github_transport_unavailable$"):
        transport.request("GET", "user")


def test_admission_revoked_after_branch_creation_stops_next_write(connected):
    runtime, _, fake, backend, token = connected
    action, _ = propose(connected)

    def revoke(method, endpoint):
        backend.admitted = lambda: False

    fake.before_write = revoke
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    assert len(fake.writes) == 1
    assert runtime.get(action.action_id, token)["state"] == "uncertain"


def test_unexpected_commit_is_not_published_as_a_pr(connected):
    runtime, _, fake, _, token = connected
    action, _ = propose(connected)
    fake.extra_file = True
    with pytest.raises(PermissionError, match="changed_files_mismatch"):
        runtime.execute(action.action_id, token)
    assert [m for m, *_ in fake.writes] == ["POST", "PUT"]
    assert fake.prs == {}


def test_timeout_after_closure_reconciles_read_only(connected):
    runtime, approvals, fake, _, token = connected
    action, _ = propose(connected)
    runtime.execute(action.action_id, token)
    undo = intent(
        action.payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = runtime.propose(undo, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    fake.fail_after = "close"
    with pytest.raises(TimeoutError):
        runtime.execute(undo.action_id, token)
    assert (
        runtime.reconcile(undo.action_id, token, "1")["receipt"]["external"]["state"] == "recalled"
    )
    assert len(fake.writes) == 4


def test_simultaneous_execution_creates_only_one_pr(connected):
    from concurrent.futures import ThreadPoolExecutor

    runtime, _, fake, _, token = connected
    action, _ = propose(connected)

    def execute():
        try:
            return runtime.execute(action.action_id, token)["state"]
        except ValueError:
            return "claimed_by_other_worker"

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: execute(), range(2)))
    assert "completed" in states
    assert len(fake.writes) == 3 and len(fake.prs) == 1


@pytest.mark.parametrize("mode", ["120000", "100755", "160000"])
def test_source_requires_a_non_executable_regular_file(connected, mode):
    runtime, _, fake, _, token = connected
    fake.mode = mode
    with pytest.raises(PermissionError, match="regular_file"):
        runtime.propose(intent(PAYLOADS[2]), token)
    assert fake.writes == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../schedule.json"),
        ("path", ".github/workflows/schedule.json"),
        ("base_branch", "main/../other"),
        ("repository", "acme/../jobs"),
        ("interval_path", ("pipelines", "")),
    ],
)
def test_target_paths_are_operator_pinned_and_strict(connected, field, value):
    _, _, _, backend, _ = connected
    with pytest.raises(ValueError):
        GitHubScheduleTarget.model_validate({**backend.target.model_dump(), field: value})


def test_already_existing_action_branch_is_never_overwritten(connected):
    runtime, _, fake, _, token = connected
    action, view = propose(connected)
    fake.refs[view["intent"]["delivery_binding"]["branch"]] = BASE
    with pytest.raises(ValueError, match="reference exists"):
        runtime.execute(action.action_id, token)
    assert len(fake.writes) == 1
    assert fake.file_sha.get(HEAD) is None


def test_unbound_retry_preserves_approved_base_after_repository_changes(connected):
    runtime, _, fake, _, token = connected
    action, view = propose(connected)
    fake.refs["main"] = "f" * 40
    replay = runtime.propose(action, token)
    assert replay["intent"]["delivery_binding"]["base_sha"] == BASE
    assert replay["approval_id"] == view["approval_id"]
    assert fake.writes == []


def test_deadline_prevents_any_later_mutation(connected, monkeypatch):
    runtime, _, fake, backend, token = connected
    action, _ = propose(connected)
    ticks = [0]
    original = fake.request

    def request(*args, **kwargs):
        result = original(*args, **kwargs)
        ticks[0] += 121
        return result

    fake.request = request
    monkeypatch.setattr("generaldataagent.business.github.monotonic", lambda: ticks[0])
    with pytest.raises(TimeoutError, match="deadline"):
        runtime.execute(action.action_id, token)
    assert fake.writes == []
