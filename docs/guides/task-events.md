# Task progress events

The local task journal exposes fixed progress metadata from the certified metric graph. It is
an opt-in pilot adapter, separate from audit evidence, LangGraph streams and checkpoint state.
It does not expose SQL, user questions, result rows, model reasoning or exception messages.

## Assembly

Create a `TaskEventJournal(Path(...))` in an operator-controlled directory. Pass the **same**
instance as `task_events=` to `MetricAnalysisRuntime(...)` and `create_app(...)`, together with
a `SessionBroker`. Do not put this database in the artifact content tree or an agent workspace.
The production constructors reject this local adapter until a qualified assembly exists.

The graph writes `started`, then `phase` events for `discover`, `plan`, `analyze`, `validate`,
and `deliver`, followed by `finished` with `completed`, `failed`, or `needs_human`. A phase event
means that phase was entered; it does not prove the phase succeeded. Exceptions produce a fixed
`interrupted` event, without the exception body. Interrupted tasks remain nonterminal.

## Reading and reconnecting

```http
GET /v1/analysis/tasks/{task_id}/events?after=0&limit=100
Authorization: Bearer <short-lived-session>
x-user-id: <authenticated-user>
x-tenant-id: <authenticated-tenant>
x-project-id: <authenticated-project>
x-data-domain: <requested-domain>
```

The route requires `analysis:run` and the original creator/tenant/project, plus all original task
domains in the requested domain headers. Private task progress is not project-shared. Missing,
expired, and unauthorized task journals all return 404 after session checks. Authentication and
scope-header checks use the existing API middleware. Service sessions with business constraints
cannot use the route. Responses disable caching.

`TaskEventPage` has `schema_version`, `task_id`, `events`, `next_cursor`, `latest_sequence`,
`has_more`, and `terminal`. Events have a stable task-local `sequence` starting at 1. Continue
with `after=next_cursor`; while `has_more` is true, read the next page. Stop polling only after
`terminal=true` **and** all pages are drained. A later empty page preserves the cursor. A cursor
past the latest sequence is rejected with 409; page sizes outside 1–200 are rejected with 422.
This is a finite JSON polling interface. The separate [bounded SSE endpoint](task-streaming.md)
adds live metadata and progress replay. The separate
[task service](task-submission.md) adds durable background submission for local development.

The analysis `task_id` differs from a caller-supplied checkpoint `thread_id`; the resume route
continues to take `thread_id`. Synchronous analysis returns task ID with its result; the separate
task service returns it with HTTP 202 before execution for submission-time progress discovery.

## Recovery and limits

SQLite transactions serialize writers across connections; read pages use a single consistent
snapshot. A restart preserves event sequences. Retried graph nodes may emit the same phase again
with a new sequence. Do not infer exactly-once execution from event counts. Protected checkpoints
still govern node recovery and execution leases.

If a completed checkpoint exists but its terminal event failed to write, authenticated resume can
append the missing terminal event without rerunning completed nodes. If a graph node failed, normal
checkpoint resume continues remaining work. A repeated resume of a completed journal adds no event.
This is not a distributed transaction between the journal, checkpoints, audit or side effects.

The default retention is seven days (maximum 30), and the default event budget is 1,000 per task
(configurable from 2 to 10,000). Expired tasks become unreadable immediately, before deletion.
Operators call `purge_expired()` to delete up to 1,000 expired tasks per batch and protect backups
according to local policy. There is no automatic cleanup scheduler or legal-hold support in this
pilot journal. Use a dedicated database; unknown schema versions are refused.

An unavailable journal or exhausted event budget fails the opted-in execution rather than silently
skipping progress. A task may require operator intervention after retention expiry or budget
exhaustion. Disable journal injection to restore the original path; keep old databases for review.
The journal has no signing or tamper-proof guarantee and is not a substitute for the audit sink.

Run the synthetic end-to-end demo with:

```bash
uv run python examples/task_events_demo.py
```
