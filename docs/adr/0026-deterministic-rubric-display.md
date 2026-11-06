# ADR 0026: Show deterministic states and real provenance, not model claims

Status: accepted for the deterministic display contract; no API route, UI, live workflow or
production admission change.

## Context

Stage 14.1A–14.1C produce a validated `RubricEvaluation` and a bounded revision ledger. A reader
still needs the result in three actionable states — satisfied (满足), gap (缺口) and needs_human
(待确认) — and needs to know what backs each state. The risk is that a fluent model comment is
mistaken for independent verification, or that "producer self-check" is presented as human signoff.

There is no independent-recalculation evidence contract in this repository yet. Inventing one here
would let a display upgrade self-check into third-party truth without the signed evidence the
project requires for external gates.

## Decision

Add the versioned `RubricDisplay` contract and `render_rubric_display`.

The renderer consumes one `TaskRubric` and one `RubricEvaluation` for the same task. It refuses a
task, rubric ID or rubric digest mismatch, an incomplete or duplicated verifier set, a missing human
check, and a human check reported as `gap` (its only allowed outcomes are `satisfied` and
`needs_human`). Every evaluation check appears exactly once in `satisfied`, `gaps` or
`needs_human`, in canonical `check_id` order. The verdict is derived again from those buckets:
any gap means `fail`, otherwise any human item means `needs_human`, otherwise `pass`.

Provenance is stated explicitly and cannot be inferred upward:

| Field | Fixed value |
|---|---|
| `producer_self_check` | `true` — the deterministic verifier run is the producer's own check |
| `independent_recheck` | `not_run` — no independent evidence contract exists in this slice |
| `human_signoff` | `not_required`, `required` or `satisfied` from the rubric's named human items and the attestation check |
| `model_commentary_authoritative` | `false` |

The display accepts no model text. A model comment may be rendered beside it as non-authoritative
annotation, but it cannot enter this contract, change a bucket or change the verdict. Frozen
contract fields and a digest over every grouped item and the provenance declaration make later
tampering visible.

## Consequences

A reader sees the same three states the evaluator produced, plus an explicit statement that an
independent recalculation has not run. Human attestation is still a declaration by an existing
authority, not business truth. Adding a real independent-recheck display requires a separately
signed evidence contract and a new versioned field; it is not simulated here by a model or by
scoring.

This slice does not add an API route or UI, call a model, perform a revision, write approvals or
audit, resolve metrics, or change production admission. `independent_review=not_run`,
`production_accepted=false`.
