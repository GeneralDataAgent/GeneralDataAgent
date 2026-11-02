# ADR 0016: Separate cancellation requests from confirmed execution stops

Status: Accepted for the local metric adapter, 2026-11-03.

Running work cannot be declared stopped merely because a user requests cancellation, a viewer
disconnects, or a worker lease expires. Old workers ignoring new fields could resume cancelled work.

Use queue/schema v2 and record/page contracts v2. A creator-authorized command cancels inactive work
atomically, but marks running work `cancel_requested`. Keep its current lease while the worker exits;
only owned settlement then records `cancelled`. Serialize cancellation and completion in SQLite.
Never reclaim requested work or automatically confirm an orphan. Preserve current session, budget,
checkpoint and artifact controls. Provide an opt-in `GuardedSQLiteReadOnlyExecutor` with a request-local VM guard
(the legacy executor stays unchanged for frozen benchmark protocol hashes) and close
the connection before acknowledgement; other blocking execution remains pending until it returns.

Require explicit offline v1 migration with all old processes stopped, reject active jobs and validate
encrypted bindings before transactional DDL. Historical v1 constructors reject the new DB version;
current readers recheck version per transaction. No transparent downgrade or rolling upgrade is offered.

Consumers use authoritative queue state and distinguish pending from confirmed stop. This adds no
production qualification, provider kill mechanism, orphan reconciliation or signed stop evidence.
