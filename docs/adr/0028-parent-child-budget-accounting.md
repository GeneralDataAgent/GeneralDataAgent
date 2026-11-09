# ADR 0028: Bound child execution inside a parent task budget

Status: accepted as an opt-in deterministic accounting library; no API, worker runtime,
dynamic subagent, route chooser, or production admission change.

## Context

Stage 15 requires the parent task total budget to reserve, settle, and release token, tool,
scan, cost, and runtime resources atomically; failed retries must count actual usage; a
cancelled child cannot be derived again; and missing usage must remain unknown rather than
being recorded as zero. The existing `TaskBudgetLedger` remains the task-level coordinator and
is unchanged; this slice does not route model choices or grant new tool permissions.

## Decision

Add the versioned child-budget contracts and `ChildBudgetBook` in `routing/child_budgets.py`.
A reservation is a YAML-free, JSON-canonical `ChildBudgetReservation` with `bytes` persisted in
a dedicated SQLite table. The reservation digest covers the parent task, child identifier,
reservation identifier, and bounded amounts. `ChildBudgetBook.reserve()` creates one row per
parent/child, or refuses a different reservation for the same child. `settle()` requires
complete measured usage within every reservation bound and records retries in the same terminal
settlement state. Partial telemetry may be recorded without becoming settled. `release()`,
`unknown()`, and `fail()` close a child with unknown usage only. `cancel()` sets a permanent
cancelled state that blocks later telemetry, settlement, and re-reservation.

Every settlement is closed under one exact reservation digest. Terminal settlements cannot be
re-settled or reused; a closed child cannot be reactivated; and digest, parent-scope,
over-reservation, and incomplete-usage errors fail closed to `ChildBudgetRefused`.

## Consequences

Callers can distinguish reserved bounds, partial telemetry, settled measured usage, released
capacity, failed work, cancelled work, and missing measurement. Unknown usage is represented as
`source=unknown` `None` values, never as zero. Complete usage without an error remains the only
path to `settled`.

This slice does not integrate the book into `TaskBudgetLedger`, spawn children, add
concurrency, launch dynamic subagents, modify production readiness, or claim an end-to-end
resource saving. Stage 15.1C and the API/UI integration remain separate. `independent_review=
not_run`, `production_accepted=false`.
