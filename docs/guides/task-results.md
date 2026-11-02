# Task lists and completed results

The authenticated task API exposes metadata pages and a renderable completed `DeliveryPackage`.
It reuses the local durable task service and remains rejected by production assembly.

## Listing an exact domain view

`GET /v1/analysis/tasks?limit=50` returns `MetricTaskPage` v1 with `tasks` and `next_cursor`.
The existing bearer, user, tenant, project and requested domain headers apply. `analysis:run` is
required. List membership is **the exact canonical domain set requested**, scoped to the creator,
tenant and project. A broader domain view is not a union of narrower views. Individual status/result
reads can still cover an original task's smaller domain set when the authenticated principal matches.

Pages contain only `MetricTaskRecord` metadata, never query text, stored session references or results.
Results are oldest-first by insertion order. Page limits are 1–100. Continue with the opaque cursor;
refresh without a cursor to discover new submissions. There is no global count or other-user scan.

Cursors are encrypted/authenticated, bound to the exact scope and expire five minutes after the first
page. A new valid session for the same scope can continue a cursor. The upper insertion boundary is
fixed; new tasks cannot appear midway through paging. Metadata statuses remain current per page and
expired jobs disappear. Removing/replacing the upper anchor invalidates the cursor, preventing rowid
reuse from admitting newly submitted tasks. A changed scope, malformed cursor, expired cursor or
missing anchor returns 409; restart from the first page. Overlong cursors or invalid limits return 422.

This is not a frozen inventory or a guarantee that metadata cannot change between pages. SQLite
reads are consistent within each page. No database migration is needed for this local v1 queue.

## Reading a completed result

`GET /v1/analysis/tasks/{task_id}/result` returns `MetricTaskResult` v1 containing `task_id`,
`thread_id`, `status=completed`, `validation` and `delivery`. The existing typed delivery includes
the table, chart instructions, narrative and evidence references. Artifact references are not public
download URLs; this endpoint does not add an attachment download route.

The queue must say completed, its encrypted context must authorize the current creator/scope, and
the protected checkpoint must have no pending nodes. Task ID, original actor, full MetricQuery,
validation task/status and the reconstructed fixed metric delivery must agree. Every evidence file
must belong to that task with its canonical artifact URI, declared size and verified content hash.
Table data and chart JSON are rechecked against the delivery; preserved narrative content is checked
when present. Governed stores also apply their export authorization/DLP policy.

The service rechecks current session authorization before and after reads and between evidence files.
Original worker-session expiry does not deny a new authorized creator session. Execution time budget
expiry does not erase an otherwise retained completed report. Result reads do not invoke/resume the
graph, reserve task budget, rerun SQL, call a model, append progress or change job attempts. The protected
checkpoint lease is briefly acquired for a consistent guarded read, then released.

Unknown, expired or unauthorized tasks/checkpoints/artifacts return 404 after authentication. Incomplete
tasks, malformed/inconsistent checkpoints and unavailable/corrupt evidence return a fixed 409 response;
raw exception messages and partial results are never returned. A result read never repairs evidence by
reexecuting a task. Retention applies independently to the queue, checkpoints and artifacts.

The factory receives a `MetricTaskExecution` whose `claim` can be a read-only `MetricTaskContext`
(record, original actor and query), without a worker lease or stored grant. Factories must use only
these common bindings to construct a runtime and attach the provided guard. Existing worker claims
retain their extra lease/grant fields. The read path does not call execution methods on that runtime.

These are producer evidence integrity checks, not independent correctness acceptance. No production
qualification, cancellation, cross-project sharing or raw artifact download is added here. The separate
[bounded SSE endpoint](task-streaming.md) streams metadata and persisted progress, never result rows.

Run `uv run python examples/task_submission_demo.py` for API acceptance, a separate worker process,
reopened task listing and authenticated final table retrieval, using only synthetic data.
