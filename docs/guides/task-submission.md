# Durable local metric tasks

The opt-in metric task service separates HTTP acceptance from execution. A supervised worker
polls an encrypted SQLite queue; HTTP requests never launch an in-memory background analysis.
This local development adapter is rejected by production API assembly.

## Assembly and authentication

Construct `MetricTaskQueue` with an operator-provided 32-byte AES key and a dedicated database,
plus `SQLiteSessionBroker`, `TaskBudgetLedger`, `TaskEventJournal`, and `MetricTaskService`.
Keep keys and state databases outside agent workspaces and artifact content directories.

The trusted runtime factory receives `MetricTaskExecution`. It returns a `MetricAnalysisRuntime`
using the deployment's existing `ProtectedSqliteSaver`, the exact service journal, budget ledger
and execution guard, and an `ExecutionComposition` bound to the claim's actor/task ID, with trace
ID equal to task ID. Existing catalog, tool, artifact and audit controls still apply. The service
validates these bindings. Existing progress plus an empty replacement checkpoint store refuses
a fresh rerun, preventing a deployment mistake from silently repeating completed SQL.

The queue stores an encrypted internal session digest, never a bearer token. Workers recheck the
live broker at graph boundaries and check budget/lease before and after nodes. Protected checkpoint
writes recheck the bound execution guard as well. Revocation, expiry,
missing permissions and scope mismatches stop execution. Digests cannot authenticate as HTTP
bearer tokens. Sessions last at most 15 minutes; delayed work may need explicit retry with a fresh
session. Production workload delegation is a later qualification, not indefinite impersonation.

## HTTP contract

Existing bearer and identity/domain headers apply, including `analysis:run`. User identity never
comes from the JSON body. Supply every task domain using repeated `x-data-domain` headers.

| Method and path | Behavior |
|---|---|
| `POST /v1/analysis/tasks` | MetricQuery body plus `Idempotency-Key`; HTTP 202 after durable acceptance |
| `GET /v1/analysis/tasks/{task_id}` | Creator-scoped metadata; unknown/expired/inaccessible tasks return 404 after authentication |
| `GET /v1/analysis/tasks` | Bounded exact-domain-view [task listing](task-results.md) with opaque cursors |
| `GET /v1/analysis/tasks/{task_id}/result` | Guarded completed [delivery read](task-results.md), without resuming work |
| `POST /v1/analysis/tasks/{task_id}/retry` | HTTP 202 for an interrupted task below its attempt limit, using a freshly checked session |
| `GET /v1/analysis/tasks/{task_id}/events` | Existing [progress replay](task-events.md), immediately usable after acceptance |

`MetricTaskRecord` v1 contains task/thread IDs, status, attempts, creation/expiry timestamps and a
fixed failure code. Initial status is queued, attempts zero; server-generated IDs persist across
retries. Same normalized actor scope/key returns the same task; a changed query returns 409.
Completed tasks are returned without rerunning. Keys permit 1–128 ASCII letters, digits, dot,
underscore, colon and dash. Payloads are capped at 64 KiB. Running and terminal tasks cannot retry.

Journal registration and queue acceptance are separate transactions. A failed HTTP response may
already have queued the task; repeat the exact request/key to discover it. Retry or worker repairs
a missing journal registration. Existing synchronous endpoints remain compatible. Legacy checkpoint
resume takes thread ID; queue retry takes task ID. Workers own queue retries; do not invoke legacy
resume concurrently for a queued task.

The service adds metadata, graph progress, listing and completed JSON delivery reads, not raw artifact
downloads, cancellation or a workbench. [Bounded SSE](task-streaming.md) adds live metadata and journal
replay. Queue status includes admission/attempt/resource failures; journal events describe
graph execution. Use queue status as the job state, including when a terminal resource failure leaves
an interrupted graph journal.

## Worker, recovery and limits

```bash
uv run python -m generaldataagent.services.metric_task_worker \
  --development --factory your_operator_module:build_service --check-config
uv run python -m generaldataagent.services.metric_task_worker \
  --development --factory your_operator_module:build_service --once
```

Without `--once`, polling defaults to two seconds (0.2–60 configurable). SIGINT/SIGTERM drain the
current execution and stop polling. Factories are operator code, never request/agent parameters.
Imports start no service. Logs contain fixed status metadata. CLI requires explicit local mode
and a SQLite broker. The operator factory owns connection/key lifetime and closes its resources.

Atomic claims use an opaque token, periodic renewal and fencing. A replaced worker cannot renew,
checkpoint subsequent node results or settle the new claim. Abrupt exit leaves work recoverable
after lease expiry. Queue and checkpoint leases are independent; an existing checkpoint lease can
safely interrupt a reclaim. Interrupted nodes may run again if they never completed a checkpoint.

Budget registration never resets usage or the original execution deadline. Execution budget starts
with the first worker, not while queued. No cross-store atomicity or exactly-once effects are claimed.
This adapter executes read-only metrics and evidence generation; publishing remains in the approved
outbox. Defaults are 24-hour retention, 60-second leases, three attempts and 100 pending tasks per
exact actor scope. Operators call bounded `purge_expired()` batches; expiry denies reads before
deletion. Mutable queue state is not a signed audit log. Schema upgrades require explicit migration.

```bash
uv run python examples/task_submission_demo.py
```

The synthetic demo accepts via authenticated API, closes the original assembly, executes a separate
worker process, then reopens state to observe the same completed task and seven events. Public
disposable fixture keys are exclusively for synthetic data; no model/external service is called.
Rollback disables task assembly and stops the supervised worker, retaining databases and keys.
