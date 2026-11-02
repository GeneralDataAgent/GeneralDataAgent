# Explicit cancellation of local metric tasks

`POST /v1/analysis/tasks/{task_id}/cancel` requires a current `analysis:run` bearer session,
matching identity/domain headers, and access as the original task creator. It returns HTTP 202
with **current metadata**, not a promise that active work has stopped. Unknown, expired or
inaccessible tasks return 404 after authentication; completed/failed/needs-human tasks return 409.
The service is opt-in, shares the existing service gate, and remains unavailable in production.

## State and consumer behavior

| Before request | After request | Stop confirmation |
|---|---|---|
| queued, interrupted | cancelled | No active execution; transaction records both timestamps |
| running | cancel_requested | Only the current unexpired worker lease can acknowledge after execution unwinds |
| cancel_requested, cancelled | unchanged | Repeated requests are idempotent |
| completed, failed, needs_human | unchanged, HTTP 409 | Finished outcomes cannot be retroactively cancelled |

Task records and task-list envelopes are **v2.0**. Records include nullable `cancel_requested_at`
and `cancelled_at`; a requested stop has no `cancelled_at`. SSE `task` frames carry v2 records.
Progress events, results, stream-end frames and encrypted request bodies retain their own v1
formats. Clients must explicitly support v2 status/metadata before this cutover. Queue metadata
is authoritative; a graph's earlier finished/interrupted journal entry does not confirm a stop.

SSE stays open for `cancel_requested`, then drains journal history before ending on `cancelled`.
Disconnecting an SSE viewer never cancels execution. Cancelled tasks cannot retry or expose a
completed result; repeating the original submission/key returns the same cancelled task. Submit a
new key for new work. Already created local evidence is retained under existing retention rules.

## Propagation and limits

Worker guards check cancellation before current-session authorization and before/after nodes and
protected checkpoint writes. Use `generaldataagent.tools.sqlite_guarded.GuardedSQLiteReadOnlyExecutor` in the local worker
factory (as in `examples/task_submission_demo.py`) for in-flight SQL interruption. This adapter checks the request-local guard through
its VM progress callback, throttled to 50 ms between checks. On denial it interrupts SQLite and
closes the connection before returning the error. This is a cooperative VM check, **not** a hard
wall-clock bound for arbitrary tools, blocking I/O, SQLite lock waits, UDFs or external providers.
The legacy `SQLiteReadOnlyExecutor` remains unchanged to preserve frozen benchmark source hashes.
It and other adapters retain boundary checks and wait for their execution to return. No model/provider
kill capability is claimed.

The lease stays renewable while work unwinds. Settlement checks the lease and cancellation request
in one transaction, so cancellation wins if its transaction commits before completion settlement.
If completion commits first, cancellation returns 409. No new worker claims a requested stop.
If the worker crashes or loses its lease, the request remains **unconfirmed**, including after
restart/lease expiry; neither timeout nor fencing proves underlying work stopped. It continues to
count against pending capacity until retention expires. Operators must investigate stopped-process
evidence; this slice provides no automatic orphan acknowledgement or force-cancel endpoint.

## Offline v1 cutover

Queue database `PRAGMA user_version` is now **2**. New readers/workers reject v1 unless explicitly
migrating; historical v1 constructors reject v2. Current readers check version on every transaction.
This is an offline upgrade, not a rolling deployment compatibility scheme.

1. Stop **all** API processes, readers and workers, including old binaries that already opened v1.
   A database lock cannot prove that an old worker is not executing SQL in another database.
2. Back up the stopped queue, checkpoint, journal, budget and session stores with their keys.
   Drain/reconcile all running jobs first: migration refuses them even if their lease has expired.
   Do not edit status/lease fields to bypass this refusal.
3. In trusted operator code, open `MetricTaskQueue(path, encryption_key=existing_key,
   migrate_v1_offline=True)` **once**. Never accept this option from an API/agent request.
   It checks the expected v1 columns, decrypts and validates every request with its bindings,
   refuses active/unknown states or residual leases, adds nullable metadata, and advances version
   under one exclusive transaction. Failed validation or DDL rolls back; ciphertext is preserved.
4. Remove the migration option and start only v2-compatible API/readers/workers. Reusing the option
   against v2 fails. Existing sessions still expire normally and budgets/attempts do not reset.

No real operator data is migrated by repository tests. Rollback disables task assembly and stops
workers, retaining v2 data and keys. Do not run an old binary against v2, downgrade its version flag,
or restore a pre-cancellation backup into active service: that could resurrect cancelled work.

## Synthetic verification

```bash
uv run python -m examples.task_cancel_demo
uv run pytest tests/test_stage11_task_cancellation.py tests/test_stage11_task_stream_http.py
```

The real loopback HTTP demo observes requested then confirmed state while a controlled tool is
released, consumes both over SSE, and verifies retry/result refusal. Tests separately interrupt a
real long-running SQLite query, exercise owner/grant failures, completion races, orphan refusal and
migration rollback. They make zero model calls and are development evidence, not production admission.
