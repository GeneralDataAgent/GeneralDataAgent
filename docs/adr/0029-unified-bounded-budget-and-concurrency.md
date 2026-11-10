# ADR 0029: Bound concurrent children inside one atomic parent budget

Status: accepted as a deterministic in-process control library; no dynamic subagent, worker
runtime or production admission change.

## Context

Stage 15.1C must allow at most two independent read-only children while keeping one fixed
delegation depth. Timeout, partial failure, revocation, and parent cancellation converge.
Reservations and usage settlement must not let a late worker or a partial observation write a
contradictory state.

## Decision

The public operation is a frozen `GovernorJob` with model-free callables. A governor validates
the fixed child set and an optional synchronous admission predicate, then reserves every child
in one SQLite `BEGIN IMMEDIATE` transaction. Each child runs in its own daemon thread under both
a per-child timeout derived from its reservation and a stage deadline.

A successful stage records partial telemetry for every child in one transaction and returns a
`StageOutcome`; children remain `telemetry_recorded` until a separate settlement call supplies
complete measured usage. Missing usage stays `unknown`, never zero, and incomplete usage cannot
settle.

An exception, a timeout, or observation above a reservation closes every child as `failed` in
one transaction, preserving known usage and marking the rest unknown. Parent cancellation or
the stage deadline closes every child as `cancelled`. Both failure paths raise
`ChildExecutionRefused`; the governor does not return a partial outcome. Late worker results are
discarded because only the governor writes ledger state after the stage closes.

The design is deliberately in-process. It has no stage-control table, no materialized
cancellation flag in SQLite, and no lease fencing; a daemon thread cannot be forcibly stopped.
Multi-process lease fencing and stale-worker recovery remain Stage 11.4 or Stage 16 work. The
caller must not treat this library as a durable worker runtime.

## Consequences

The governor can converge in every covered in-process fault mode without creating a worker
runtime. Production routes remain adapters over committed data, not the governor itself.
`independent_review=not_run` and `production_accepted=false`.
