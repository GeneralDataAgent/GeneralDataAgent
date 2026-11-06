# ADR 0023: Declare versioned evidence rubrics before any revision

Status: accepted for deterministic local evaluation; production admission and automatic revision unchanged.

## Context

Stage 14 asks for a task rubric with required data source, time range, metric version, method,
delivery table/chart, citations and human-confirmation items, each mapped to an executable verifier.
The system already has `ValidationResult`, `DeliveryPackage` and `ExecutionReceipt`, but those do not
express an operator-defined acceptance contract for a specific task. A rubric must not be a free-form
model prompt or a self-score: an unknown verifier, a missing required check, a mixed source snapshot
or an unacknowledged human item must fail closed, and a `needs_human` state must never be reported as
a pass.

## Decision

Add v1 `TaskRubric` with a fixed schema version, task ID, metric name/version, ISO time range, named
`RubricSourceRequirement` bindings, one `RubricMethodRequirement` and one
`RubricDeliveryRequirement`. The delivery requirement names required claim types,
human-confirmation items and an explicit verifier list. v1 requires the complete set of seven
registered deterministic verifiers; unknown, duplicate, incomplete or omitted IDs are refused at
contract construction, so a rubric cannot silently skip an acceptance check.

`rubric_digest` binds the complete declaration through canonical JSON. Construction refuses an
unparseable or inverted ISO time range, duplicate sources/verifiers/human items and a digest
mismatch. A rubric change creates a new immutable contract; old evaluations are never rewritten.

The evaluator consumes one exact rubric, one `DeliveryPackage`, a tuple of `SourceBinding` observations
and one `ExecutionReceipt`. It re-derives the rubric digest, checks that the receipt and all delivery
evidence belong to the rubric's task, that the declared source and method verifiers are consistent,
and that receipt result references are delivery evidence, then runs every registered verifier:

| Verifier | Deterministic check |
|---|---|
| `rubric.required_checks_present` | All declared IDs are registered and baseline checks are present |
| `rubric.time_range_bound` | ISO range is ordered and stated exactly in at least one delivery claim |
| `rubric.metric_version_bound` | Delivery metric, method ID/version match the rubric and a passed receipt |
| `rubric.sources_single_snapshot` | Observed sources match the rubric and receipt; no cross-task result refs |
| `rubric.claims_grounded` | Every claim cites same-task evidence and required claim types are present |
| `rubric.table_and_chart_evidence` | Table/chart reference same-task delivery evidence |
| `rubric.human_items_acknowledged` | Human-confirmation items require a named `human_attestation` whose check codes name every item |

A `RubricEvaluation` records ordered per-verifier outcomes (`satisfied`, `gap`, `needs_human`), an
overall `pass`/`fail`/`needs_human` verdict, an evaluation digest and fixed facts
`producer_self_check_only=true`, `model_judgement_used=false`, `revision_performed=false`. A gap wins
over a human item; a human item wins over a pass. Tampering with either digest or the outcome/verdict
mapping is rejected at construction. Checks are emitted in canonical `check_id` order, independent of
the verifier declaration order.

## Consequences

Operators can declare and verify a bounded acceptance contract without a model, a repair loop, a
database write or an API change. Missing time range, mixed source snapshots, missing claim types,
unknown/unregistered verifiers, cross-task evidence and missing named human attestation all have
explicit deterministic outcomes. A `human_attestation` receipt is still an attestation recorded
by an existing authority, not independent truth or business correctness; the rubric never upgrades
`producer_self_check` to an independent result.

This slice does not bind the three business templates, does not perform a bounded revision, does not
show a UI, does not integrate with the metric graph, does not add approval or audit writes and does not
change production admission. Those remain separate Stage 14 slices. The capability catalog records it
as an opt-in deterministic library port, not an accepted report or product deployment.
