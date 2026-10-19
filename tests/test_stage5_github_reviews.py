from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_stage5_actions import PAYLOADS, intent

from generaldataagent.api.app import create_app
from generaldataagent.business.actions import ActionControl, BusinessActionRuntime
from generaldataagent.business.github_review import GitHubReviewBackend, GitHubReviewTarget
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.security.identity import SessionBroker


class ReviewGitHub:
    def __init__(self):
        self.issues, self.writes = {}, []
        self.actor, self.repo_id, self.admitted = 11, 22, True
        self.fail_after = None

    def request(self, method, path, body=None):
        if path == "user":
            return {"id": self.actor}
        if path == "repos/acme/reviews":
            return {
                "id": self.repo_id,
                "full_name": "acme/reviews",
                "archived": False,
                "has_issues": True,
            }
        if method == "GET":
            return deepcopy(self.issues[int(path.rsplit("/", 1)[1])])
        self.writes.append((method, path, deepcopy(body)))
        if method == "POST":
            number = len(self.issues) + 1
            self.issues[number] = {
                "id": number + 100,
                "number": number,
                "url": f"https://api.github.com/repos/acme/reviews/issues/{number}",
                "user": {"id": self.actor},
                "state": "open",
                "locked": False,
                "labels": [],
                "assignees": [],
                "milestone": None,
                **body,
            }
        else:
            number = int(path.rsplit("/", 1)[1])
            self.issues[number].update(body)
        if self.fail_after == method:
            raise TimeoutError("lost acknowledgement")
        return deepcopy(self.issues[number])


@pytest.fixture(params=[PAYLOADS[0], PAYLOADS[1], PAYLOADS[3]], ids=lambda p: p.kind)
def connected(request, tmp_path):
    payload = request.param
    fake = ReviewGitHub()
    backend = GitHubReviewBackend(
        target=GitHubReviewTarget(
            repository="acme/reviews",
            repository_id=22,
            actor_id=11,
            kind=payload.kind,
            configuration_version="v1",
        ),
        transport=fake,
        admitted=lambda: fake.admitted,
    )
    broker, approvals = SessionBroker(), SQLiteApprovalStore(tmp_path / "approvals")
    runtime = BusinessActionRuntime(
        tmp_path / "actions",
        broker=broker,
        approvals=approvals,
        controls=(
            ActionControl(
                payload.kind, payload.kind, "operator", "t", "p", frozenset({"sales"}), enabled=True
            ),
        ),
        external_backends={(payload.kind, payload.kind): backend},
    )
    token = broker.issue(
        user_id="alice",
        tenant_id="t",
        project_id="p",
        data_domains=frozenset({"sales"}),
        permissions=frozenset({"business:propose", "business:read", "actions:resume"}),
    )
    return runtime, approvals, broker, token, backend, fake, payload


def proposed(connected):
    runtime, approvals, _, token, _, _, payload = connected
    action = intent(payload)
    view = runtime.propose(action, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    return action, view


def test_exact_review_request_replay_and_separate_compensation(connected):
    runtime, approvals, _, token, backend, fake, payload = connected
    action = intent(payload)
    view = runtime.propose(action, token)
    assert not fake.writes
    assert view["intent"]["delivery_binding"]["kind"] == payload.kind
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    result = runtime.execute(action.action_id, token)
    assert result["receipt"]["external_delivery"] is True
    assert fake.issues[1]["body"] == view["intent"]["delivery_binding"]["body"]
    assert runtime.execute(action.action_id, token) == result
    assert runtime.propose(action, token)["receipt"] == result["receipt"]
    undo = intent(
        payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = runtime.propose(undo, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    assert runtime.execute(undo.action_id, token)["receipt"]["external"]["state"] == "recalled"
    assert [c[0] for c in fake.writes] == ["POST", "PATCH"]
    assert fake.issues[1]["state"] == "closed"
    assert runtime.verify_audit()


@pytest.mark.parametrize("failure", ["actor", "repo", "admission", "configuration"])
def test_identity_configuration_revocation_denies_writes(connected, failure):
    runtime, _, _, token, backend, fake, _ = connected
    action, _ = proposed(connected)
    if failure == "actor":
        fake.actor = 99
    elif failure == "repo":
        fake.repo_id = 99
    elif failure == "admission":
        fake.admitted = False
    else:
        backend.target = backend.target.model_copy(update={"configuration_version": "v2"})
    with pytest.raises(PermissionError):
        runtime.execute(action.action_id, token)
    assert not fake.writes


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "changed"),
        ("body", "changed"),
        ("locked", True),
        ("labels", [{"name": "live"}]),
        ("assignees", [{"id": 42}]),
        ("milestone", {"id": 42}),
        ("pull_request", {}),
        ("url", "https://api.github.com/repos/other/reviews/issues/1"),
    ],
)
def test_changed_review_is_never_closed(connected, field, value):
    runtime, approvals, _, token, _, fake, payload = connected
    action, _ = proposed(connected)
    runtime.execute(action.action_id, token)
    fake.issues[1][field] = value
    undo = intent(
        payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = runtime.propose(undo, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    with pytest.raises(PermissionError):
        runtime.execute(undo.action_id, token)
    assert len(fake.writes) == 1


def test_lost_ack_quarantines_and_read_only_recovery(connected):
    runtime, _, _, token, _, fake, _ = connected
    action, _ = proposed(connected)
    fake.fail_after = "POST"
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    with pytest.raises(ValueError):
        runtime.execute(action.action_id, token)
    assert (
        runtime.reconcile(action.action_id, token, "1")["receipt"]["external"]["issue_number"] == 1
    )
    assert len(fake.writes) == 1


def test_concurrent_execution_does_not_duplicate_review(connected):
    runtime, _, _, token, _, fake, _ = connected
    action, _ = proposed(connected)

    def execute(_):
        try:
            return runtime.execute(action.action_id, token)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute, range(2)))
    assert len(fake.writes) == 1


def test_api_typed_issue_reconciliation(connected):
    runtime, _, broker, token, _, fake, _ = connected
    action, _ = proposed(connected)
    fake.fail_after = "POST"
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    client = TestClient(create_app(business_actions=runtime, session_broker=broker))
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "alice",
        "x-tenant-id": "t",
        "x-project-id": "p",
        "x-data-domain": "sales",
    }
    url = f"/v1/business/drafts/{action.action_id}/reconcile"
    assert (
        client.post(url, json={"issue_number": 1, "pull_number": 1}, headers=headers).status_code
        == 422
    )
    assert client.post(url, json={"issue_number": True}, headers=headers).status_code == 422
    assert client.post(url, json={"issue_number": 1}, headers=headers).status_code == 200
    assert len(fake.writes) == 1


def test_lost_close_ack_reconciles_without_repeating_closure(connected):
    runtime, approvals, _, token, _, fake, payload = connected
    action, _ = proposed(connected)
    runtime.execute(action.action_id, token)
    undo = intent(
        payload, operation="compensate", compensates=action.action_id, expected_revision=1
    )
    view = runtime.propose(undo, token)
    approvals.approve(UUID(view["approval_id"]), approver_id="bob")
    fake.fail_after = "PATCH"
    with pytest.raises(TimeoutError):
        runtime.execute(undo.action_id, token)
    assert (
        runtime.reconcile(undo.action_id, token, "1")["receipt"]["external"]["state"] == "recalled"
    )
    assert len(fake.writes) == 2


def test_receipt_cannot_be_adopted_from_a_different_author(connected):
    runtime, _, _, token, _, fake, _ = connected
    action, _ = proposed(connected)
    fake.fail_after = "POST"
    with pytest.raises(TimeoutError):
        runtime.execute(action.action_id, token)
    fake.issues[1]["user"]["id"] = 99
    with pytest.raises(PermissionError):
        runtime.reconcile(action.action_id, token, "1")
    assert len(fake.writes) == 1


def test_review_artifacts_are_bounded_nonexecuting_and_fenced(connected):
    runtime, _, _, token, _, fake, payload = connected
    action = intent(payload)
    view = runtime.propose(action, token)
    import json

    body = view["intent"]["delivery_binding"]["body"]
    document = json.loads(body.split("```json\n")[1].split("\n```")[0])
    assert document["deployment_allowed"] is False
    assert document["status"] == "pending_human_review"
    if payload.kind == "dashboard":
        assert all(p["query_enabled"] is False for p in document["dashboard_draft"]["panels"])
    assert not fake.writes
