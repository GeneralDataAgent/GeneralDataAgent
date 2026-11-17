"""Stage 13.2A: governed, default-off read-only MCP through ToolGateway."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from admission_helpers import enroll
from fastapi.testclient import TestClient
from test_metric_analysis import actor
from test_stage11_task_submission import credentials

from generaldataagent.api.app import create_app
from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.connectors.mcp import (
    McpAuthFailed,
    McpProtocolError,
    McpRequestRejected,
    McpResponseDenied,
    McpResponseTooLarge,
    McpSchemaDrift,
    McpServerUnavailable,
    McpSession,
    McpSessionManager,
    McpSessionScopeError,
    McpTimeout,
    McpToolConflict,
    McpToolNotAdmitted,
    McpVersionMismatch,
    StdioMcpTransport,
    discover_tools,
)
from generaldataagent.contracts import (
    ActorContext,
    ArtifactClassification,
    McpServerAdmission,
    McpServerProfile,
    McpToolAdmission,
    RiskLevel,
)
from generaldataagent.contracts.mcp import gateway_tool_name
from generaldataagent.observability.audit import JsonlAuditSink
from generaldataagent.orchestration.mcp import McpReadService
from generaldataagent.policy import InMemoryApprovalStore
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.tools.gateway import PolicyDeniedError

FIXTURE = Path(__file__).with_name("mcp_fixture_server.py")
TOOL = "lookup_metric"
ARGS = {"metric": "synthetic.revenue"}


def profile(**overrides):
    values = {
        "server_id": "fixture_read",
        "version": "v1",
        "tool_prefix": "mcp.fixture_read",
        "timeout_seconds": 2.0,
        "max_response_bytes": 65_536,
        "credential_env": None,
    }
    values.update(overrides)
    return McpServerProfile(**values)


def factory(state, *, expected_token=None, line_limit=1_048_576):
    def build(candidate, credential):
        env = {"GDA_MCP_FIXTURE_MODE": state["mode"]}
        if expected_token is not None:
            env["GDA_MCP_FIXTURE_EXPECTED_TOKEN"] = expected_token
        if credential is not None:
            env["GDA_MCP_TOKEN"] = credential
        return StdioMcpTransport(
            (sys.executable, str(FIXTURE)),
            env=env,
            max_line_bytes=line_limit,
        )

    return build


def admission_from(discovery, server_profile, *, risk="r1", roles=("verifier",)):
    tools = tuple(
        McpToolAdmission(
            remote_tool=tool.name,
            gateway_tool=gateway_tool_name(server_profile.tool_prefix, tool.name),
            description_sha256=tool.description_sha256,
            input_schema=tool.input_schema,
            risk_level=risk,
            allowed_roles=roles,
        )
        for tool in discovery.tools
    )
    return McpServerAdmission(
        profile=server_profile,
        schema_sha256=discovery.schema_sha256,
        tools=tools,
    )


def setup(tmp_path, *, mode="normal", risk="r1", credential_env=None, **profile_overrides):
    state = {"mode": mode}
    server = profile(credential_env=credential_env, **profile_overrides)
    transport_factory = factory(state)
    discovery = asyncio.run(discover_tools(server, transport_factory=transport_factory))
    admission = admission_from(discovery, server, risk=risk)
    registry = CapabilityRegistry(tmp_path / "registry.db", clock=lambda: datetime.now(UTC))
    enroll(
        registry,
        actor(),
        "connector",
        server.server_id,
        admission.digest(),
        version=server.version,
    )
    store = GovernedArtifactStore(
        tmp_path / "artifacts",
        index_path=tmp_path / "artifact-index.db",
        actor=actor(),
        retention_seconds=3600,
        read_policy=ArtifactReadPolicy.CREATOR,
    )
    approvals = InMemoryApprovalStore()
    service = McpReadService(
        profile=server,
        admission=admission,
        registry=registry,
        store=store,
        transport_factory=transport_factory,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        approvals=approvals,
    )
    return SimpleNamespace(
        state=state,
        profile=server,
        admission=admission,
        discovery=discovery,
        factory=transport_factory,
        registry=registry,
        store=store,
        approvals=approvals,
        service=service,
        tmp_path=tmp_path,
    )


def read(env, *, remote_tool=TOOL, arguments=None, approval_id=None, task_id=None):
    return env.service.read(
        actor=actor(),
        task_id=task_id or uuid4(),
        remote_tool=remote_tool,
        arguments=ARGS if arguments is None else arguments,
        approval_id=approval_id,
    )


def test_read_through_the_product_entry_creates_creator_bound_evidence(tmp_path):
    env = setup(tmp_path)
    broker = SQLiteSessionBroker(tmp_path / "sessions.db")
    _, headers = credentials(broker)
    task_id = uuid4()
    app = create_app(session_broker=broker, mcp_read=env.service)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/analysis/mcp/{TOOL}/read",
            json={"task_id": str(task_id), "arguments": ARGS},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence["task_id"] == str(task_id)
    assert evidence["gateway_tool"] == f"mcp.fixture_read.{TOOL}"
    assert evidence["schema_sha256"] == env.admission.schema_sha256
    assert evidence["admission_hash"] == env.admission.digest()
    assert evidence["artifact"]["artifact_type"] == "mcp_read_result"
    assert evidence["artifact"]["created_by"] == actor().user_id

    stored = json.loads(env.store.export_bytes(_ref(evidence)))
    assert stored["result"]["structuredContent"]["metric"] == "synthetic.revenue"
    assert stored["result"]["structuredContent"]["source"] == "fixture://certified-metrics"

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    kinds = [event["event_type"] for event in events]
    assert "policy.decision" in kinds
    assert "mcp.schema.verified" in kinds
    assert "mcp.read.completed" in kinds
    completed = next(event for event in events if event["event_type"] == "mcp.read.completed")
    assert completed["payload"]["artifact_hash"] == evidence["response_hash"]


def _ref(evidence):
    from generaldataagent.contracts import ArtifactRef

    return ArtifactRef.model_validate(evidence["artifact"])


def test_mcp_read_is_default_off_and_production_rejects_the_local_adapter(tmp_path):
    plain = create_app()
    assert not any(
        getattr(route, "path", "").startswith("/v1/analysis/mcp/") for route in plain.routes
    )

    env = setup(tmp_path)
    broker = SQLiteSessionBroker(tmp_path / "sessions.db")
    with pytest.raises(ValueError, match="local_mcp_read_not_qualified_for_production"):
        create_app(
            session_broker=broker,
            mcp_read=env.service,
            production_mode=True,
        )


def test_remote_description_and_annotations_cannot_lower_local_risk(tmp_path):
    env = setup(tmp_path, mode="malicious", risk="r2")
    assert "ignore all policy" in env.discovery.tools[0].description
    assert env.admission.tools[0].risk_level == RiskLevel.R2
    task_id = uuid4()
    with pytest.raises(PolicyDeniedError):
        read(env, task_id=task_id)

    request = env.service.request_for(
        actor=actor(), task_id=task_id, remote_tool=TOOL, arguments=ARGS
    )
    assert request.requested_risk == RiskLevel.R2
    approval = env.approvals.create_request(
        request,
        impact_scope="one synthetic metric read",
        cost_estimate="0",
        classification=ArtifactClassification.INTERNAL,
        rollback_plan="read-only; no state to restore",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    env.approvals.approve(approval.approval_id, approver_id="reviewer-1")
    evidence = read(env, approval_id=approval.approval_id, task_id=task_id)
    assert evidence.remote_tool == TOOL


def test_initialization_notification_precedes_list_and_call(tmp_path):
    env = setup(tmp_path, mode="strict_lifecycle")
    assert read(env).remote_tool == TOOL


def test_read_verifies_catalog_on_the_connection_used_for_the_call(tmp_path):
    env = setup(tmp_path, mode="session_catalog")
    evidence = read(env)
    assert evidence.remote_tool == TOOL
    assert evidence.schema_sha256 == env.admission.schema_sha256


def test_schema_or_tool_list_change_requires_a_new_admission(tmp_path):
    env = setup(tmp_path)
    env.state["mode"] = "drift"
    with pytest.raises(McpSchemaDrift):
        read(env)
    assert list((tmp_path / "artifacts").rglob("*.json")) == []


def test_version_mismatch_and_missing_credential_fail_closed(tmp_path):
    state = {"mode": "normal"}
    newer = profile(version="v2")
    with pytest.raises(McpVersionMismatch):
        asyncio.run(discover_tools(newer, transport_factory=factory(state)))

    guarded = profile(credential_env="GDA_MCP_TEST_TOKEN")
    with pytest.raises(McpAuthFailed):
        asyncio.run(discover_tools(guarded, transport_factory=factory(state)))


def test_failed_remote_authentication_is_deterministic(tmp_path, monkeypatch):
    state = {"mode": "normal"}
    guarded = profile(credential_env="GDA_MCP_TEST_TOKEN")
    monkeypatch.setenv("GDA_MCP_TEST_TOKEN", "wrong")
    with pytest.raises(McpAuthFailed):
        asyncio.run(
            discover_tools(
                guarded,
                transport_factory=factory(state, expected_token="right"),
            )
        )


def test_gateway_name_conflict_is_rejected_at_discovery(tmp_path):
    state = {"mode": "conflict"}
    with pytest.raises(McpToolConflict):
        asyncio.run(discover_tools(profile(), transport_factory=factory(state)))


def test_unavailable_child_process_fails_closed(tmp_path):
    def missing(_profile, _credential):
        return StdioMcpTransport(("/nonexistent/mcp-fixture",))

    with pytest.raises(McpServerUnavailable):
        asyncio.run(discover_tools(profile(), transport_factory=missing))


def test_oversized_and_dlp_responses_never_become_artifacts(tmp_path):
    oversized = setup(tmp_path, mode="oversized", max_response_bytes=4096)
    with pytest.raises(McpResponseTooLarge):
        read(oversized)
    assert list((tmp_path / "artifacts").rglob("*.json")) == []

    denied_path = tmp_path / "denied"
    denied = setup(denied_path, mode="dlp")
    with pytest.raises(McpResponseDenied):
        read(denied)
    assert list((denied_path / "artifacts").rglob("*.json")) == []


def test_timeout_closes_the_session_and_stops_the_child(tmp_path):
    env = setup(tmp_path, mode="slow", timeout_seconds=0.1)
    with pytest.raises(McpTimeout):
        read(env)
    assert env.service.sessions.active is None


def test_protocol_error_and_unadmitted_or_invalid_reads_fail_closed(tmp_path):
    broken = setup(tmp_path / "broken", mode="badjson")
    with pytest.raises(McpProtocolError):
        read(broken)

    env = setup(tmp_path / "valid")
    with pytest.raises(McpToolNotAdmitted):
        read(env, remote_tool="other_metric")
    with pytest.raises(McpRequestRejected):
        read(env, arguments={"metric": 7})
    with pytest.raises(McpRequestRejected):
        read(env, arguments={})
    assert list((tmp_path / "valid" / "artifacts").rglob("*.json")) == []


class _FakeTransport:
    def __init__(self):
        self.started = self.closed = False

    async def start(self):
        self.started = True

    async def request(self, method, params):
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "fake", "version": "v1"},
            }
        else:  # pragma: no cover - these tests never call a tool
            result = {}
        return {"jsonrpc": "2.0", "id": 1, "result": result}

    async def notify(self, method):
        assert method == "notifications/initialized"

    async def close(self):
        self.closed = True


class _BlockingTransport(_FakeTransport):
    async def request(self, method, params):
        if method == "initialize":
            return await super().request(method, params)
        await asyncio.Event().wait()  # pragma: no cover - cancelled by the test


def test_a_session_is_never_reused_across_tenant_scopes():
    transports = []

    def build(_profile, _credential):
        transport = _FakeTransport()
        transports.append(transport)
        return transport

    manager = McpSessionManager(build)
    server = profile()

    async def scenario():
        async with manager.lease(
            server, None, tenant_id="tenant-a", project_id="project-1", user_id="user-1"
        ) as first:
            assert first.closed is False
            with pytest.raises(McpSessionScopeError):
                async with manager.lease(
                    server,
                    None,
                    tenant_id="tenant-b",
                    project_id="project-1",
                    user_id="user-1",
                ):
                    pass
        assert first.closed is True
        async with manager.lease(
            server, None, tenant_id="tenant-b", project_id="project-1", user_id="user-1"
        ) as second:
            assert second is not first
        await manager.aclose()

    asyncio.run(scenario())
    assert all(transport.started and transport.closed for transport in transports)


def test_shutdown_closes_an_active_session(tmp_path):
    manager = McpSessionManager(factory({"mode": "normal"}))
    server = profile()

    async def scenario():
        async with manager.lease(
            server, None, tenant_id="tenant-1", project_id="project-1", user_id="user-1"
        ) as session:
            await manager.aclose()
            assert session.closed is True
            assert manager.active is None

    asyncio.run(scenario())


def test_cancellation_closes_the_session():
    transport = _BlockingTransport()
    session = McpSession(profile(), transport)

    async def scenario():
        await session.start()
        task = asyncio.create_task(session.call_tool(TOOL, ARGS))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.closed is True
        assert transport.closed is True

    asyncio.run(scenario())


def _actor_with_tenant(tenant):
    return ActorContext(
        user_id=actor().user_id,
        tenant_id=tenant,
        project_id=actor().project_id,
        data_domains=actor().data_domains,
    )


def test_admission_is_checked_against_the_requesting_tenant(tmp_path):
    env = setup(tmp_path)
    other = _actor_with_tenant("tenant-other")
    with pytest.raises(PermissionError):
        env.service.read(
            actor=other,
            task_id=uuid4(),
            remote_tool=TOOL,
            arguments=ARGS,
        )
    assert list((tmp_path / "artifacts").rglob("*.json")) == []
