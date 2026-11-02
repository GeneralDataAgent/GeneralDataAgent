import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor, build_runtime, query
from test_stage11_task_submission import assembly, credentials

from generaldataagent.api.app import create_app
from generaldataagent.contracts import ActorContext
from generaldataagent.orchestration.task_streams import (
    MetricTaskStreams,
    TaskStreamBusy,
    TaskStreamLimits,
)


def parse_frames(text):
    frames = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":") or not line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()
        if fields.get("event"):
            fields["data"] = json.loads(fields["data"])
            frames.append(fields)
    return frames


def stream_client(service, streams=None, enabled=None):
    return TestClient(
        create_app(
            session_broker=service.broker,
            task_events=service.events,
            metric_tasks=service,
            task_streams=streams,
            service_enabled=enabled,
        )
    )


async def connected():
    return False


def test_completed_task_sse_replays_fixed_metadata_and_no_result_or_query(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "sse")
        service.run_once()
        streams = MetricTaskStreams(service)
        response = stream_client(service, streams).get(
            f"/v1/analysis/tasks/{record.task_id}/stream", headers=headers
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        frames = parse_frames(response.text)
        progress = [f for f in frames if f["event"] == "progress"]
        assert [f["data"]["sequence"] for f in progress] == list(range(1, 8))
        assert [f["id"] for f in progress] == [f"{record.task_id}:{i}" for i in range(1, 8)]
        assert frames[0]["event"] == "task" and "id" not in frames[0]
        assert frames[-1]["data"] == {"schema_version": "1.0", "reason": "terminal", "after": 7}
        for forbidden in ("SELECT", "order_count", "delivery_package", "session_reference", token):
            assert forbidden not in response.text
        assert streams._connections == {}


def test_frame_budget_and_last_event_id_resume_have_no_silent_gaps(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "resume")
        service.run_once()
        streams = MetricTaskStreams(service, limits=TaskStreamLimits(max_frames=3))
        client = stream_client(service, streams)
        url = f"/v1/analysis/tasks/{record.task_id}/stream"
        collected = []
        cursor = None
        for _ in range(6):
            actual = headers | ({"Last-Event-ID": cursor} if cursor else {})
            response = client.get(url, headers=actual)
            assert response.status_code == 200
            frames = parse_frames(response.text)
            progress = [f for f in frames if f["event"] == "progress"]
            collected.extend(f["data"]["sequence"] for f in progress)
            if progress:
                cursor = progress[-1]["id"]
            if frames[-1]["data"]["reason"] == "terminal":
                break
        assert collected == list(range(1, 8))
        assert streams._connections == {}
        invalid = headers | {"Last-Event-ID": f"{uuid4()}:2"}
        assert client.get(url, headers=invalid).status_code == 409
        assert client.get(url, headers=headers, params={"after": 99}).status_code == 409
        assert (
            client.get(
                url, headers=headers | {"Last-Event-ID": f"{record.task_id}:2"}, params={"after": 3}
            ).status_code
            == 409
        )
        assert client.get(url, headers=headers, params={"after": -1}).status_code == 422
        assert client.get(url, headers=headers | {"Last-Event-ID": "x" * 81}).status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("other",)},
    ],
)
def test_sse_owner_scope_is_checked_before_headers_and_reveals_no_existence(tmp_path, change):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "scope")
        stranger = ActorContext.model_validate(actor().model_dump() | change)
        _, headers = credentials(service.broker, stranger)
        client = stream_client(service)
        actual = client.get(f"/v1/analysis/tasks/{record.task_id}/stream", headers=headers)
        missing = client.get(f"/v1/analysis/tasks/{uuid4()}/stream", headers=headers)
        assert actual.status_code == missing.status_code == 404
        assert actual.json() == missing.json()


def test_sse_auth_permission_header_scope_and_default_gate(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "auth")
        url = f"/v1/analysis/tasks/{record.task_id}/stream"
        client = stream_client(service)
        assert client.get(url).status_code == 401
        _, denied = credentials(service.broker, permissions=frozenset())
        assert client.get(url, headers=denied).status_code == 403
        assert client.get(url, headers=headers | {"x-user-id": "forged"}).status_code == 403
        assert TestClient(create_app()).get(url).status_code == 503


@pytest.mark.parametrize("loss", ["revoke", "expiry", "service_gate"])
def test_open_stream_stops_after_auth_or_service_changes(tmp_path, loss):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    gate = [True]

    def enabled():
        return gate[0]

    original = build_runtime(tmp_path)
    with assembly(tmp_path, original, clock=lambda: now) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "live-auth")
        streams = MetricTaskStreams(service, enabled=enabled)
        prepared = streams.prepare(record.task_id, actor(), token)

        async def consume():
            nonlocal now
            generator = prepared.iterate(connected)
            first = await anext(generator)
            assert parse_frames(first)[0]["event"] == "task"
            if loss == "revoke":
                service.broker.revoke(token)
            elif loss == "expiry":
                now += timedelta(seconds=900)
            else:
                gate[0] = False
            rest = "".join([frame async for frame in generator])
            assert parse_frames(rest) == [
                {
                    "event": "stream_end",
                    "data": {"schema_version": "1.0", "reason": "unavailable", "after": 0},
                }
            ]

        asyncio.run(consume())
        assert streams._connections == {}


def test_disconnect_and_explicit_generator_close_release_capacity_without_polling(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "disconnect")
        streams = MetricTaskStreams(
            service, limits=TaskStreamLimits(max_connections=1, max_per_principal=1)
        )
        prepared = streams.prepare(record.task_id, actor(), token)
        calls = []
        read = service.events.read

        def counted(*args, **kwargs):
            calls.append(1)
            return read(*args, **kwargs)

        service.events.read = counted

        async def consume():
            generator = prepared.iterate(connected)
            await anext(generator)
            before = len(calls)
            await asyncio.sleep(0.03)
            assert len(calls) == before  # a slow consumer starts no independent polling loop
            await generator.aclose()

        asyncio.run(consume())
        assert streams._connections == {}
        another = streams.prepare(record.task_id, actor(), token)

        async def gone():
            return True

        async def empty():
            return [frame async for frame in another.iterate(gone)]

        assert asyncio.run(empty()) == []
        assert streams._connections == {}


def test_capacity_is_per_principal_not_domain_variant_and_abandoned_setup_expires(tmp_path):
    original = build_runtime(tmp_path)
    ticks = [0.0]
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "capacity")
        limits = TaskStreamLimits(max_connections=2, max_per_principal=1, max_seconds=0.1)
        streams = MetricTaskStreams(service, limits=limits, clock=lambda: ticks[0])
        prepared = streams.prepare(record.task_id, actor(), token)
        with pytest.raises(TaskStreamBusy):
            streams.prepare(record.task_id, actor(), token)
        broad = actor().model_copy(update={"data_domains": ("commerce", "finance")})
        broad_token, _ = credentials(service.broker, broad)
        with pytest.raises(TaskStreamBusy):
            streams.prepare(record.task_id, broad, broad_token)
        response = stream_client(service, streams).get(
            f"/v1/analysis/tasks/{record.task_id}/stream", headers=headers
        )
        assert response.status_code == 429 and response.headers["retry-after"] == "1"
        ticks[0] = 6
        replacement = streams.prepare(record.task_id, actor(), token)
        prepared.close()
        assert len(streams._connections) == 1
        replacement.close()
        assert streams._connections == {}


def test_duration_and_poll_failure_are_fixed_control_frames(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "duration")
        limits = TaskStreamLimits(max_seconds=0.15, poll_seconds=0.02, keepalive_seconds=0.1)
        streams = MetricTaskStreams(service, limits=limits)
        url = f"/v1/analysis/tasks/{record.task_id}/stream"
        response = stream_client(service, streams).get(url, headers=headers)
        assert parse_frames(response.text)[-1]["data"]["reason"] == "duration"
        assert streams._connections == {}
        prepared = streams.prepare(record.task_id, actor(), token)

        def broken(*_):
            raise RuntimeError("private database diagnostic")

        streams._snapshot = broken

        async def fail():
            return "".join([f async for f in prepared.iterate(connected)])

        text = asyncio.run(fail())
        assert "private database" not in text
        assert parse_frames(text)[-1]["data"]["reason"] == "unavailable"
        assert streams._connections == {}


def test_global_admission_limit_covers_distinct_principals(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        first = service.submit(query(), actor(), token, "first")
        other = actor().model_copy(update={"user_id": "other"})
        other_token, _ = credentials(service.broker, other)
        second = service.submit(query(), other, other_token, "second")
        streams = MetricTaskStreams(
            service, limits=TaskStreamLimits(max_connections=1, max_per_principal=1)
        )
        prepared = streams.prepare(first.task_id, actor(), token)
        with pytest.raises(TaskStreamBusy):
            streams.prepare(second.task_id, other, other_token)
        prepared.close()
        replacement = streams.prepare(second.task_id, other, other_token)
        replacement.close()
        assert streams._connections == {}


def test_queue_completion_between_journal_reads_does_not_drop_tail(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "interleaving")
        streams = MetricTaskStreams(service)
        read = service.events.read
        triggered = []

        def interleaved(*args, **kwargs):
            page = read(*args, **kwargs)
            if kwargs.get("limit") == 50 and not triggered:
                triggered.append(True)
                service.run_once()
            return page

        service.events.read = interleaved
        response = stream_client(service, streams).get(
            f"/v1/analysis/tasks/{record.task_id}/stream", headers=headers
        )
        frames = parse_frames(response.text)
        assert [f["data"]["sequence"] for f in frames if f["event"] == "progress"] == list(
            range(1, 8)
        )
        assert frames[-1]["data"]["reason"] == "terminal"


def test_api_stream_wiring_must_share_service_and_kill_switch(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        streams = MetricTaskStreams(service)
        with pytest.raises(ValueError, match="share_task_service"):
            create_app(task_streams=streams)
        with pytest.raises(ValueError, match="share_task_service"):
            stream_client(service, streams, enabled=lambda: True)


def test_journal_failure_before_headers_is_fixed_503_without_leaking_diagnostics(tmp_path):
    original = build_runtime(tmp_path)
    with assembly(tmp_path, original) as (service, _):
        token, headers = credentials(service.broker)
        record = service.submit(query(), actor(), token, "storage-down")
        streams = MetricTaskStreams(service)
        with sqlite3.connect(service.events.path) as db:
            db.execute("DROP TABLE task_events")
        response = stream_client(service, streams).get(
            f"/v1/analysis/tasks/{record.task_id}/stream", headers=headers
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "task_stream_unavailable"}
        assert streams._connections == {}


def test_idle_keepalive_uses_fixed_comment_without_advancing_cursor(tmp_path):
    original = build_runtime(tmp_path)
    ticks = [0.0]

    def ticking():
        ticks[0] += 0.05
        return ticks[0]

    with assembly(tmp_path, original) as (service, _):
        token, _ = credentials(service.broker)
        record = service.submit(query(), actor(), token, "keepalive")
        limits = TaskStreamLimits(max_seconds=1, poll_seconds=0.01, keepalive_seconds=0.1)
        streams = MetricTaskStreams(service, limits=limits, clock=ticking)
        prepared = streams.prepare(record.task_id, actor(), token)

        async def read():
            return "".join([f async for f in prepared.iterate(connected)])

        text = asyncio.run(read())
        assert ": keep-alive\n\n" in text
        frames = parse_frames(text)
        assert frames[-1]["data"] == {"schema_version": "1.0", "reason": "duration", "after": 1}
        assert len([f for f in frames if "id" in f]) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"max_seconds": 0},
        {"poll_seconds": float("nan")},
        {"max_frames": 1},
        {"max_connections": True},
        {"max_connections": 1, "max_per_principal": 2},
    ],
)
def test_stream_limits_are_bounded(options):
    with pytest.raises(ValueError):
        replace(TaskStreamLimits(), **options)
