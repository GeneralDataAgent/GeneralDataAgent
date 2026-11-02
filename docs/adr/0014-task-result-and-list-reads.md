# ADR 0014: Read completed tasks through current authorization and retained evidence

Status: Accepted for the local task adapter, 2026-11-03.

## Context

Task acceptance and progress are insufficient for a workbench to reopen task history and render its
final output. Reusing resume for result reads could consume budget or execute incomplete work.
Unsigned client offsets could cross scope, and mutable/deleted SQLite rowids can destabilize paging.

## Decision

Expose creator-scoped metadata listing for an exact requested-domain view, with encrypted scope-bound
cursors and an insertion upper anchor. Reject invalidated/expired cursors rather than silently mixing
new tasks into an old page sequence. Keep current statuses and retention filtering explicit.

Read completed results directly from protected checkpoints through a read-only task context and live
authorization guard. Verify the request binding, completed graph, validation and retained same-task
evidence; never invoke/resume work or reset execution budgets to retrieve a report. Governed artifacts
also apply export policy. Missing or inconsistent evidence denies the whole response.

## Consequences

New authorized sessions can read retained reports after the worker grant/execution budget expires.
Factories receive common actor/task/query bindings for both execution and reading; result contexts
carry no worker lease or saved grant. The local queue layout remains v1. Page membership is bounded,
while metadata may change and expired rows may disappear. No production or independent correctness
acceptance, raw artifact download, SSE or cancellation is implied.
