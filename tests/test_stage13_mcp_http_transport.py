"""Stage 13.2B: loopback streamable-HTTP MCP transport and its failure semantics."""

import asyncio
import datetime as dt
import json
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from admission_helpers import enroll
from mcp_http_fixture_server import http_fixture
from test_metric_analysis import actor

from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.connectors.mcp import (
    HttpMcpTransport,
    McpAuthFailed,
    McpProtocolError,
    McpRequestRejected,
    McpResponseTooLarge,
    McpServerUnavailable,
    McpSession,
    McpSessionBusy,
    McpSessionManager,
    McpTimeout,
    discover_tools,
    http_transport_factory,
)
from generaldataagent.contracts import (
    Budget,
    McpServerAdmission,
    McpServerProfile,
    McpToolAdmission,
)
from generaldataagent.contracts.mcp import gateway_tool_name, parse_loopback_endpoint
from generaldataagent.observability.audit import JsonlAuditSink
from generaldataagent.orchestration.mcp import McpReadService
from generaldataagent.policy import InMemoryApprovalStore
from generaldataagent.policy.budgets import BudgetExceededError, TaskBudgetLedger

TOOL = "lookup_metric"
ARGS = {"metric": "synthetic.revenue"}


def http_profile(endpoint, **overrides):
    values = {
        "server_id": "fixture_http",
        "transport": "http",
        "endpoint": endpoint,
        "version": "v1",
        "tool_prefix": "mcp.fixture_http",
        "timeout_seconds": 2.0,
        "max_response_bytes": 65_536,
    }
    values.update(overrides)
    return McpServerProfile(**values)


def admission_from(discovery, server_profile):
    tools = tuple(
        McpToolAdmission(
            remote_tool=tool.name,
            gateway_tool=gateway_tool_name(server_profile.tool_prefix, tool.name),
            description_sha256=tool.description_sha256,
            input_schema=tool.input_schema,
            risk_level="r1",
            allowed_roles=("verifier",),
        )
        for tool in discovery.tools
    )
    return McpServerAdmission(
        profile=server_profile,
        schema_sha256=discovery.schema_sha256,
        tools=tools,
    )


def setup(tmp_path, endpoint, *, budgets=None, **overrides):
    server_profile = http_profile(endpoint, **overrides)
    discovery = asyncio.run(
        discover_tools(server_profile, transport_factory=http_transport_factory)
    )
    admission = admission_from(discovery, server_profile)
    registry = CapabilityRegistry(tmp_path / "registry.db", clock=lambda: dt.datetime.now(dt.UTC))
    enroll(
        registry,
        actor(),
        "connector",
        server_profile.server_id,
        admission.digest(),
        version=server_profile.version,
    )
    store = GovernedArtifactStore(
        tmp_path / "artifacts",
        index_path=tmp_path / "artifact-index.db",
        actor=actor(),
        retention_seconds=3600,
        read_policy=ArtifactReadPolicy.CREATOR,
    )
    service = McpReadService(
        profile=server_profile,
        admission=admission,
        registry=registry,
        store=store,
        transport_factory=http_transport_factory,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        approvals=InMemoryApprovalStore(),
        budgets=budgets,
    )
    return service, admission, store


def read(service, **kwargs):
    return service.read(
        actor=actor(),
        task_id=kwargs.pop("task_id", uuid4()),
        remote_tool=kwargs.pop("remote_tool", TOOL),
        arguments=kwargs.pop("arguments", ARGS),
        **kwargs,
    )


def test_profile_accepts_only_plain_http_endpoints_on_an_ip_loopback():
    valid = http_profile("http://127.0.0.1:8765/mcp")
    assert valid.transport == "http"
    assert valid.endpoint == "http://127.0.0.1:8765/mcp"
    assert parse_loopback_endpoint("http://[::1]:8765/mcp").host_header == "[::1]:8765"

    rejected = [
        {"transport": "stdio", "endpoint": "http://127.0.0.1:8765/mcp"},
        {"transport": "http", "endpoint": None},
        {"transport": "http", "endpoint": "http://example.com/mcp"},
        {"transport": "http", "endpoint": "https://127.0.0.1:8765/mcp"},
        {"transport": "http", "endpoint": "http://user:pw@127.0.0.1:8765/mcp"},
        {"transport": "http", "endpoint": "http://127.0.0.1/mcp"},
        {"transport": "http", "endpoint": "http://localhost:8765/mcp"},
        {"transport": "http", "endpoint": "http://127.0.0.1:8765/mcp?token=1"},
        {"transport": "http", "endpoint": "http://127.0.0.1:8765/mcp#frag"},
        {"transport": "http", "endpoint": "http://127.0.0.1:8765/mcp path"},
        {"transport": "http", "endpoint": "http://127.0.0.1:8765//evil.example"},
        {"transport": "http", "endpoint": "http://127.0.0.1:8765/a/../b"},
    ]
    for override in rejected:
        with pytest.raises(ValueError):
            http_profile(**override)


def test_http_discovery_and_read_create_creator_bound_evidence(tmp_path):
    with http_fixture() as (server, endpoint):
        service, admission, store = setup(tmp_path, endpoint)
        evidence = read(service)

    assert evidence.remote_tool == TOOL
    assert evidence.server_id == "fixture_http"
    assert evidence.schema_sha256 == admission.schema_sha256
    stored = json.loads(store.export_bytes(evidence.artifact))
    assert stored["result"]["structuredContent"]["source"] == "fixture://http-certified-metrics"
    assert server.requests >= 4  # initialize, initialized, tools/list, tools/call
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert "mcp.schema.verified" in [event["event_type"] for event in events]


def test_sse_stream_yields_the_response_after_a_server_notification(tmp_path):
    with http_fixture("sse") as (_server, endpoint):
        discovery = asyncio.run(
            discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
        )
        service, _admission, _store = setup(tmp_path, endpoint)
        evidence = read(service)
    assert discovery.tools[0].name == TOOL
    assert evidence.remote_tool == TOOL


def test_redirect_unknown_media_type_and_compression_are_refused():
    with http_fixture("redirect") as (_server, endpoint):
        with pytest.raises(McpServerUnavailable) as redirect:
            asyncio.run(
                discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
            )
        assert redirect.value.code == "mcp_redirect_refused"

    for mode in ("wrong_type", "gzip"):
        with http_fixture(mode) as (_server, endpoint), pytest.raises(McpProtocolError):
            asyncio.run(
                discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
            )


def test_oversized_content_length_and_chunked_bodies_are_refused(tmp_path):
    with http_fixture("oversized") as (_server, endpoint), pytest.raises(McpResponseTooLarge):
        asyncio.run(
            discover_tools(
                http_profile(endpoint, max_response_bytes=4096),
                transport_factory=http_transport_factory,
            )
        )
    with (
        http_fixture("oversized_chunked") as (_server, endpoint),
        pytest.raises(McpResponseTooLarge),
    ):
        asyncio.run(
            discover_tools(
                http_profile(endpoint, max_response_bytes=4096),
                transport_factory=http_transport_factory,
            )
        )


def test_truncated_body_fails_closed_without_an_artifact(tmp_path):
    with http_fixture("disconnect") as (_server, endpoint):
        with pytest.raises(McpServerUnavailable) as failure:
            asyncio.run(
                discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
            )
        assert failure.value.code == "mcp_server_closed"
    assert list((tmp_path / "artifacts").rglob("*.json")) == []


def test_slow_response_times_out_and_closes_the_session(tmp_path):
    with http_fixture("normal", delay=1.0) as (_server, endpoint):
        service, _admission, _store = setup(tmp_path, endpoint, timeout_seconds=0.2)
        with pytest.raises(McpTimeout):
            read(service)
        assert service.sessions.active is None
    assert list((tmp_path / "artifacts").rglob("*.json")) == []


def test_cancellation_closes_the_http_session_and_socket():
    with http_fixture("normal", delay=1.0) as (_server, endpoint):
        transport = HttpMcpTransport(endpoint)
        session = McpSession(http_profile(endpoint, timeout_seconds=5.0), transport)

        async def scenario():
            await session.start()
            task = asyncio.create_task(session.call_tool(TOOL, ARGS))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.closed is True
            assert transport.closed is True

        asyncio.run(scenario())


def test_requests_on_one_session_are_serialized():
    with http_fixture("normal", delay=0.15) as (server, endpoint):
        transport = HttpMcpTransport(endpoint)
        session = McpSession(http_profile(endpoint), transport)

        async def scenario():
            await session.start()
            try:
                first, second = await asyncio.gather(
                    session.call_tool(TOOL, ARGS),
                    session.call_tool(TOOL, ARGS),
                )
            finally:
                await session.close()
            return first, second

        first, second = asyncio.run(scenario())
    assert first["structuredContent"]["metric"] == "synthetic.revenue"
    assert second["structuredContent"]["metric"] == "synthetic.revenue"
    assert server.max_inflight == 1


def test_a_second_lease_is_refused_while_one_is_active():
    with http_fixture() as (_server, endpoint):
        manager = McpSessionManager(http_transport_factory)
        server_profile = http_profile(endpoint)

        async def scenario():
            async with manager.lease(
                server_profile,
                None,
                tenant_id="tenant-1",
                project_id="project-1",
                user_id="user-1",
            ):
                with pytest.raises(McpSessionBusy):
                    async with manager.lease(
                        server_profile,
                        None,
                        tenant_id="tenant-1",
                        project_id="project-1",
                        user_id="user-1",
                    ):
                        pass
            await manager.aclose()

        asyncio.run(scenario())


def test_protocol_version_and_session_header_travel_with_each_request():
    with http_fixture("session") as (server, endpoint):
        discovery = asyncio.run(
            discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
        )
    assert discovery.protocol_version == "2025-06-18"
    assert server.requests >= 3


def test_wrong_ids_missing_responses_and_bad_json_fail_closed():
    for mode in ("wrong_id", "sse_wrong_id", "no_content", "bad_json", "server_error"):
        with http_fixture(mode) as (_server, endpoint):
            error = McpProtocolError if mode != "server_error" else McpServerUnavailable
            with pytest.raises(error):
                asyncio.run(
                    discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
                )


def test_authentication_uses_the_named_environment_variable(monkeypatch):
    with http_fixture("normal", token="right") as (_server, endpoint):
        guarded = http_profile(endpoint, credential_env="GDA_MCP_HTTP_TOKEN")
        monkeypatch.setenv("GDA_MCP_HTTP_TOKEN", "wrong")
        with pytest.raises(McpAuthFailed):
            asyncio.run(discover_tools(guarded, transport_factory=http_transport_factory))
        monkeypatch.setenv("GDA_MCP_HTTP_TOKEN", "right")
        discovery = asyncio.run(discover_tools(guarded, transport_factory=http_transport_factory))
        assert discovery.server_id == "fixture_http"


def test_proxy_environment_is_ignored(monkeypatch, tmp_path):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    with http_fixture() as (_server, endpoint):
        service, _admission, _store = setup(tmp_path, endpoint)
        evidence = read(service)
    assert evidence.remote_tool == TOOL


def test_http_transport_rejects_an_injected_credential_header(tmp_path):
    with http_fixture() as (_server, endpoint):
        with pytest.raises(ValueError, match="mcp_credential_invalid"):
            HttpMcpTransport(endpoint, credential="token\r\nX-Injected: 1")
        with pytest.raises(ValueError):
            HttpMcpTransport("http://example.com/mcp")


def test_workspace_style_stdio_profiles_still_build():
    profile = McpServerProfile(
        server_id="fixture_read",
        version="v1",
        tool_prefix="mcp.fixture_read",
        timeout_seconds=2.0,
        max_response_bytes=65_536,
    )
    assert profile.transport == "stdio"
    assert profile.endpoint is None
    with pytest.raises(McpProtocolError, match="mcp_http_profile_required"):
        http_transport_factory(profile, None)
    assert Path(sys.executable).is_file()


def test_a_body_without_framing_is_read_until_close():
    with http_fixture("eof") as (_server, endpoint):
        discovery = asyncio.run(
            discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
        )
    assert discovery.tools[0].name == TOOL


@pytest.mark.parametrize(
    "mode",
    [
        "bad_status",
        "bad_content_length",
        "folded_header",
        "bad_transfer_encoding",
        "bad_chunk",
        "bad_chunk_terminator",
        "ct_params",
        "empty_body",
        "http_404",
        "bad_session",
        "event_type",
        "event_field",
        "sse_empty",
        "sse_bad_json",
    ],
)
def test_malformed_http_and_sse_framing_fails_closed(mode):
    with http_fixture(mode) as (_server, endpoint), pytest.raises(McpProtocolError):
        asyncio.run(
            discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
        )


def test_transport_state_and_request_bounds_fail_closed():
    with http_fixture() as (_server, endpoint):
        with pytest.raises(ValueError, match="mcp_response_limit_invalid"):
            HttpMcpTransport(endpoint, max_response_bytes=64)
        transport = HttpMcpTransport(endpoint)

        async def scenario():
            await transport.start()
            with pytest.raises(McpProtocolError, match="mcp_transport_already_started"):
                await transport.start()
            with pytest.raises(McpRequestRejected, match="mcp_request_too_large"):
                await transport.request("tools/call", {"metric": "x" * 300_000})
            with pytest.raises(McpRequestRejected, match="mcp_request_not_serializable"):
                await transport.request("tools/call", {"metric": {1, 2}})
            await transport.close()
            with pytest.raises(McpProtocolError, match="mcp_transport_closed"):
                await transport.request("tools/list", {})
            with pytest.raises(McpProtocolError, match="mcp_transport_closed"):
                await transport.notify("notifications/initialized")

        asyncio.run(scenario())


def test_connection_refused_is_server_unavailable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(McpServerUnavailable):
        asyncio.run(
            discover_tools(
                http_profile(f"http://127.0.0.1:{port}/mcp"),
                transport_factory=http_transport_factory,
            )
        )


def test_a_no_response_failure_is_retried_once_for_an_idempotent_read(tmp_path):
    with http_fixture(fail_once_method="tools/call") as (server, endpoint):
        service, _admission, _store = setup(tmp_path, endpoint)
        evidence = read(service)

    assert evidence.remote_tool == TOOL
    assert server.requests >= 5  # initialize, initialized, list, failed call, retried call
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    retried = [event for event in events if event["event_type"] == "mcp.transport.retried"]
    assert len(retried) == 1
    assert retried[0]["payload"]["reason"] == "mcp_server_closed"
    assert retried[0]["payload"]["attempt"] == 1
    completed = [event for event in events if event["event_type"] == "mcp.read.completed"]
    assert completed[0]["payload"]["retry_count"] == 1


def test_retries_are_bounded_to_one_attempt():
    with (
        http_fixture("close_before_response") as (server, endpoint),
        pytest.raises(McpServerUnavailable),
    ):
        asyncio.run(
            discover_tools(http_profile(endpoint), transport_factory=http_transport_factory)
        )
    assert server.requests == 2


@pytest.mark.parametrize(
    ("mode", "error", "overrides"),
    [
        ("server_error", McpServerUnavailable, {}),
        ("redirect", McpServerUnavailable, {}),
        ("wrong_id", McpProtocolError, {}),
        ("bad_json", McpProtocolError, {}),
        ("http_404", McpProtocolError, {}),
        ("disconnect", McpServerUnavailable, {}),
        ("oversized", McpResponseTooLarge, {"max_response_bytes": 4096}),
    ],
)
def test_failures_after_a_response_byte_or_an_http_status_are_not_retried(mode, error, overrides):
    with http_fixture(mode) as (server, endpoint), pytest.raises(error):
        asyncio.run(
            discover_tools(
                http_profile(endpoint, **overrides),
                transport_factory=http_transport_factory,
            )
        )
    assert server.requests == 1


def test_a_timeout_is_not_retried():
    with (
        http_fixture("normal", delay=1.0, slow_method="initialize") as (server, endpoint),
        pytest.raises(McpTimeout),
    ):
        asyncio.run(
            discover_tools(
                http_profile(endpoint, timeout_seconds=0.2),
                transport_factory=http_transport_factory,
            )
        )
    assert server.requests == 1


def test_each_retry_is_charged_to_the_task_budget(tmp_path):
    with http_fixture(fail_once_method="tools/call") as (_server, endpoint):
        task_id = uuid4()
        budgets = TaskBudgetLedger(tmp_path / "budgets.db")
        budgets.register(task_id, actor(), Budget(max_tool_calls=1))
        service, _admission, _store = setup(tmp_path, endpoint, budgets=budgets)
        with pytest.raises(BudgetExceededError):
            read(service, task_id=task_id)
    assert list((tmp_path / "artifacts").rglob("*.json")) == []
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert "mcp.transport.retried" not in [event["event_type"] for event in events]
