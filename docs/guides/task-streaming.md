# Bounded task SSE

`GET /v1/analysis/tasks/{task_id}/stream` streams application metadata for the local durable task
service. It uses the existing bearer, user/tenant/project/domain headers and `analysis:run` permission.
`metric_tasks` must be configured; default assemblies return 503 and production assembly stays refused.
No raw LangGraph events, model reasoning, prompts, SQL, result rows or credentials are streamed.

## Frames and reconnection

| Event | Payload | SSE ID |
|---|---|---|
| `task` | `MetricTaskRecord` v2, initial/current queue status and attempts | None |
| `progress` | `TaskEvent` v1, persisted phase history | `{task_id}:{sequence}` |
| `stream_end` | `TaskStreamEnd` v1: `reason` and last delivered `after` | None |

Queue status is authoritative for the job; replayed progress can be historical even when the current
task snapshot is already completed. Render phase history by sequence/time, rather than downgrading
current status when an older phase is replayed. Final results use the separate authenticated result
endpoint, not the stream. Progress and control frames are not audit or correctness evidence.

Resume with `Last-Event-ID: {task_id}:{sequence}`. Clients using the existing JSON event cursor can
instead supply `?after=<sequence>`. Supplying both requires the same sequence. Another task's ID,
malformed/conflicting cursor or a cursor ahead of the journal returns 409 before streaming. Negative
query cursors or headers over 80 characters return 422. Snapshot/keepalive/end frames never advance
the progress cursor. Drain all history before treating a terminal end as complete.

End reasons: `terminal` (queue terminal and history drained), `duration`, `frame_limit`, or
`unavailable`. Resume after duration/frame limits using the last progress ID; no events are silently
dropped. Reauthenticate when necessary. Before headers, unknown/expired/unauthorized tasks return
404 after identity checks, missing permission returns 403, invalid session returns 401, and journal
failures return a fixed 503. After headers, authorization/storage failures produce only a fixed
unavailable control frame and close, without private diagnostics or partial result data.

Browsers should use authenticated fetch streaming or the future same-origin BFF; native EventSource
cannot supply the required custom bearer/scope headers. Do not put bearer tokens in URLs.

## Bounds and lifecycle

Default `TaskStreamLimits`: 30 seconds of application iteration, 0.5-second polling, keepalive comments
after 10 idle seconds, 200 metadata/progress frames, eight connections per process and two per user/
tenant/project principal. Domain variants share the principal cap. Limits are operator-owned and
validated; application duration is capped at 60 seconds, frames at 1,000. A terminal control frame is
separate from the frame budget. There is no unbounded producer queue: a slow consumer does not start
independent polling, and each read page contains at most 50 progress events.

Authorization, required task domains, task retention and the service kill switch are rechecked at
poll boundaries and before each data frame. Ending a stream or disconnecting a client releases its
slot and does **not** cancel the underlying task. Use the separate [explicit cancellation](task-cancellation.md) command.
`cancel_requested` keeps the stream open; only confirmed `cancelled` is terminal.
Abandoned response setup slots are reaped after their application deadline plus five seconds.

Application bounds are observed when the iterator can run. They are not a hard timeout on a stalled
socket send or a global multi-process admission system. ASGI transport backpressure, reverse-proxy
connection/send timeouts and production capacity qualification remain deployment responsibilities.
Responses disable caching and request proxy buffering with `X-Accel-Buffering: no`; the proxy must
honor its own configuration. Existing request SLO middleware does not measure full stream lifetime.

`create_app` creates a controller for a configured metric task service. To override limits, pass a
`MetricTaskStreams` bound to the exact same service and service-enabled callback as `task_streams=`;
incoherent wiring is rejected. No worker/model/tool execution is triggered by opening the stream.

## Synthetic real HTTP demonstration

```bash
uv run python -m examples.task_stream_demo
uv run python -m examples.task_stream_demo --disconnect
```

An ephemeral server binds only 127.0.0.1. A real HTTP client receives queued status before releasing
the synthetic worker, then drains seven progress events. Disconnect mode closes the client early,
proves the worker still finishes, and checks that connection admission is released. The client ignores
environment proxies; regression runs set an intentionally unusable proxy to verify loopback isolation.
No real model or enterprise service is called. Environments that prohibit socket binding need permission
for this local integration test; ASGI-only tests still exercise protocol and authorization paths.

Rollback disables the local task service/stream route and retains durable jobs/journals. This slice
adds no production qualification, external publication or raw result stream.
