# ADR 0013: Separate durable metric acceptance from guarded execution

Status: Accepted for a local development adapter, 2026-11-02.

## Context

Progress replay alone cannot let a client discover a task before synchronous analysis returns.
Long tasks also need idempotent acceptance, worker ownership and checkpoint identity across restarts.
Persisting a caller-supplied actor is insufficient authorization; raw tokens do not belong in queues.

## Decision

Persist encrypted metric requests with server-owned task/thread IDs and a revocable session digest.
Expose authenticated 202 acceptance, metadata and explicit interrupted-task retry. A supervised local
worker uses renewable fenced claims and revalidates sessions at graph boundaries. Execution reuses
the original budget and protected checkpoints with exact request bindings.

A trusted factory binds task, actor, trace, journal, budgets and guard to the claim. Existing progress
plus a missing checkpoint refuses a fresh rerun. Partial acceptance and terminal-write gaps are
recoverable; no cross-database atomicity is claimed.

## Consequences

Existing synchronous endpoints remain compatible. Session expiry can interrupt delayed jobs; new
credentials require explicit retry. This is not qualified workload delegation and is rejected in
production assembly. Queue status includes admission/resource failures, while events describe graph
progress. Exactly-once effects, live SSE, cancellation, result download, listing and UI remain separate
slices. External actions stay in the existing approval/outbox service.
