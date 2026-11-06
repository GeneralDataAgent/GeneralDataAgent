# ADR 0025: Bound revisions with a pinned rubric and a retained version chain

Status: accepted for the deterministic library port; no API, UI, live workflow or production
admission change.

## Context

Stage 14.1A evaluates one delivery against a rubric and returns `pass`, `fail` or `needs_human`.
That verdict says what is wrong, but nothing bounds what happens next: a caller could re-run the
same failing path indefinitely, or silently accept a revised delivery whose evidence was never
recorded. Stage 14.1C therefore needs a control that answers only whether one more revision may be
attempted, and a record that keeps every version that was actually produced.

The control cannot trust model text and must not become an automatic repair. It also has to
distinguish a repairable gap from a cause where another attempt is wasted: a missing source, a
missing capability or an authorization denial.

## Decision

Add `BoundedRevisionController` and the versioned `RevisionLedger` contract.

The controller is constructed with one `TaskRubric` and the initial delivery/evidence digests. It
pins the task ID and rubric digest for its whole lifetime. A request carries a validated
`RubricEvaluation`, the selected `check_id`, the current input digest/evidence and the remaining
`RevisionBudget`. The controller:

* refuses an evaluation for another task, rubric or rubric digest (`revision_task_mismatch`,
  `revision_rubric_mismatch`, `revision_rubric_changed`);
* stops with `revision_rubric_satisfied` on `pass`, and `revision_requires_human` on
  `needs_human`; neither consumes a revision;
* accepts only a selected `gap` check; an unknown or non-gap check is refused;
* stops without consuming a revision for `no_source`, `no_capability` and
  `authorization_denied`, and for every cause that the routing policy already classifies as
  non-retryable;
* allows at most two revisions; the third request stops with `revision_limit_reached`;
* requires positive remaining seconds, tokens and tool calls, checked in that fixed order;
* requires the input digest and evidence set to equal the current retained version, and requires
  the previous output to be recorded before another request is authorized.

`record()` captures the produced output digest and evidence as an immutable `RevisionVersion`
whose input fields come from controller state, not from the caller. `ledger()` returns a digest-bound
`RevisionLedger` with the initial version, the complete chain and the stop reason
(`revision_open` while no terminal decision exists). Duplicate, unordered or malformed evidence
digests, an unchanged output, a broken chain or a tampered ledger digest are refused.

The controller performs no model call, no repair, no approval/audit write and no publication. It
cannot upgrade a delivery: the revised delivery must still pass `evaluate_rubric` before delivery.
The caller must supply an evaluation of the current version; the evaluation contract does not carry
a delivery digest, so the controller binds the input/evidence chain but does not re-run verifiers.
A stale gap evaluation can waste at most the two bounded revisions and cannot produce a pass.

## Consequences

Revision attempts become reviewable as an exact chain: for each attempt a reviewer sees the failure
code and cause, the full input and output digests, and both evidence sets. Budget exhaustion,
non-repairable causes and human-required outcomes are distinct stop reasons rather than a generic
"retry failed". Because the rubric digest is pinned, changing acceptance criteria requires a new
controller and ledger and never rewrites an earlier report.

This slice does not expose an API or UI, call a model, judge prose, perform the revision itself,
resolve metric aliases, write approvals or audit, or change production admission.
`independent_review=not_run`, `production_accepted=false`.
