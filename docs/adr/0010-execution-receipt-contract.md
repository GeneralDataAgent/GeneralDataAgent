# ADR 0010: Versioned execution receipt contract

Status: Proposed on 2026-10-26. The contract in
`src/generaldataagent/contracts/receipt.py` exists and is tested.
`GovernedRuleRunner.invoke()` now returns a receipt for persisted workflow
evidence. `DataScienceWorkbench.run_experiment()` now embeds a receipt in its
experiment report. Rule delivery returns a separate receipt, and the rule
Supervisor retains execution and delivery receipts. Fresh research evaluations can
opt in to step receipts without rewriting legacy reports. This
ADR is the contract review that ADR 0009 requires before adapting execution;
implementation and local tests do not constitute independent review or acceptance.

## Context

Evidence is currently scattered across several shapes that mean similar things:

- `rule.workflow.evidence` artifacts carry request, source hashes, binding hash,
  verification and computation for one rule run;
- `DeliveryPackage.evidence` carries artifact references and grounded claims;
- science reports carry `runtime_fingerprint`, metrics and checks;
- evaluation rows carry step bindings, `result_hash` and method identifiers in
  `stage_seven_acceptance.py`.

Nothing in these is wrong, but each consumer re-derives "what ran, on what,
with what verification". Worse, the same word — *verified* — covers a producer's
own self-check, a re-derivation by the same implementation, and an independently
implemented check. Collapsing those into one boolean would overstate evidence.

The risk on the other side is equally real: adding a receipt that looks
authoritative invites reading a hash as proof of origin or correctness. A content
hash proves byte identity only.

## Decision

Define one versioned `ExecutionReceipt` contract and adopt it in stages. The
contract fixes the shape now; adoption is separate work.

### Fields

- identity: `task_id`, `action`, `actor_id`, `tenant_id`, `project_id`, `trace_id`
- `sources`: named `SourceBinding` entries with `content_hash` and optional `version`
- `method`: `ExecutionMethod` with `method_id`, `method_version`, optional
  `runtime_fingerprint` and `random_seed`
- `result_refs`: `ArtifactRef` values produced by the same task
- `completeness`: `complete` / `partial` / `unknown`
- `usage`: `ObservedUsage`
- `verification`: `ReceiptVerification`

### Invariants the contract enforces

1. **Same-task references.** Every `result_refs` entry must belong to the
   receipt's `task_id`. A receipt cannot adopt another task's artifacts.
2. **Unknown is not zero.** `ObservedUsage` defaults to `None`; `telemetry_complete`
   is false unless observed token counts are present, and a partial measurement
   never produces a `total_tokens`.
3. **Verification strength is declared.** `scope` distinguishes
   `producer_self_check`, `authoritative_rederivation`,
   `independent_implementation`, `human_attestation` and `none`. A `none` scope
   cannot report `passed`, a declared scope must name its checks, and the two
   strongest scopes must name a verifier.
4. **A complete execution references its result.** `completeness="complete"`
   without `result_refs` is refused.
5. **No authenticity claim.** The contract has no `authentic`, `approved`,
   `signed_by` or reviewer-identity field. Digests identify bytes.

### Non-goals

- Not an attestation or signature scheme; the existing verified audit chain and
  release admission remain the authority for those questions.
- Not a replacement for `DeliveryPackage`; the receipt describes an execution,
  the package describes what may be delivered.
- Not a historical migration: legacy evaluation rows remain unchanged. Fresh runs
  use the opt-in adapter described below.

## Adoption plan

1. **Rule side**: derive a receipt from the existing `rule.workflow.evidence`
   fields without weakening the current task binding or source checks.
2. **Science side**: derive a receipt from the experiment report, putting the
   sandbox `runtime_fingerprint` and seed into `method`.
3. **Evaluation compatibility**: map step bindings to receipt identity, keeping
   old reports readable and never rewritten.

Each slice must keep the invariants above and must not relabel a weaker
verification as a stronger one to make an old report fit.

## Rule adapter

`GovernedRuleRunner.invoke()` returns a JSON `receipt` alongside its existing
`status`, `thread_id`, `evidence` and `production_accepted` fields. The receipt is
an inline projection of the checkpoint and the stored evidence reference; it is
not a separately persisted artifact. Existing evidence writes still pass through
the gateway and the store's byte/hash check before a receipt is returned.
`DeliveryPackage` retains its existing schema and feedback hash. Delivery adds its
own receipt in the response envelope, described below.

- Identity uses the authorized actor and resolved composition task/trace. Sources
  come from the checked checkpoint, and the method version is its workflow version.
- `RuleWorkflowServices.verification_scope` is trusted application wiring. Its
  default is `producer_self_check`; the Annex services declare
  `authoritative_rederivation`. The wrapper saves that declaration in a separate
  checkpoint channel when verification runs. Handler-returned `scope` metadata
  cannot raise the declared strength. Resuming a completed check preserves its
  stored scope even if the new runtime declares a stronger one.
- Legacy checkpoints without that channel use `producer_self_check`, ignoring any
  old handler `scope` metadata. Missing named checks produce `scope="none"` and
  `passed=false`; malformed check names are refused. Failures remain available
  through the referenced evidence, including their original failure codes.
- Completed workflows report `completeness="complete"`; other returned states
  report `partial`. Completeness describes execution, not verification strength.
  A verifier or persistence exception propagates without returning a receipt.
- Existing source names matching the receipt identifier syntax are preserved.
  If any name does not match, every source gets the identifier `s` followed by
  the first 63 hexadecimal characters of SHA-256 over its original UTF-8 name.
  Mapping every name avoids collisions between a literal name and a generated
  alias; duplicate generated identifiers are refused. Evidence retains exact
  original names and content hashes. Names and hashes are never normalized in
  the workflow's source/request/version binding.
- Usage is unknown because this adapter does not observe model/resource totals.
  It does not synthesize zeros or infer complete telemetry from gateway events.

No old evaluation reports are rewritten. Recomputing after a source update uses
a new task, preserving parent evidence; see [rule recomputation](../guides/rule-recomputation.md).

## Rule delivery and Supervisor

`RuleDeliveryRuntime.deliver()` returns an inline `receipt` next to `delivery`.
Its action is `rules.deliver`, method is `rules.delivery`, version is the checked
workflow version, and sources and verification scope come from the checkpoint.
It references exactly the package's three same-task artifacts. Actor and trace
use the authorized delivery composition. Usage remains unknown.

Each artifact retains its own gateway policy and audit event. Before calling the
store, the adapter captures the intended canonical bytes; immediate readback must
match them, so a self-consistent hash for substituted content is refused. All
three artifacts are rechecked before feedback registration. Storage failures can
leave earlier artifacts, but do not return a receipt or register feedback.

`SupervisorResult.receipts` defaults to an empty tuple for legacy payloads. The
rule Supervisor retains `rules.execute` and `rules.deliver` separately, including
an execution receipt when later roles stop or fail. A direct workflow run without
a governed execution receipt exposes only the delivery receipt. It does not
invent an execution receipt or reinterpret execution evidence as delivery. Old
readers with `extra="forbid"` require an upgrade for this additive result field.
Neither receipt grants production admission or independent-verifier status.

## Science adapter

`DataScienceWorkbench.run_experiment()` keeps its `ExperimentReport` return type
and adds an optional inline `receipt`. The existing result artifact is read back
through the store and its decoded content compared with the canonical executed
output before the report can be tracked or returned. A valid hash for a different
same-task output is insufficient. Receipts identify
the authorized actor and resolved composition task/trace, with `python.experiment`
as the action. Sources bind the immutable snapshot content and the canonical JSON
hash of the full `ExperimentSpec`, including split, estimator, preprocessing and
seed. `ExperimentSpec.content_hash()` defines that canonical serialization.

The method identifier is `science.<estimator>`; its version is the executed
fixed-template version, fingerprint is the actual injected sandbox's fingerprint,
and seed is `spec.random_seed`. `ExecutionMethod` schema 1.1 adds optional
`random_seed` (0 through 2**32-1); missing seeds remain unknown. Report schema 1.1
adds optional `receipt`. Explicit schema 1.0 JSON remains readable with the new
reader; old readers using `extra="forbid"` cannot consume the additive fields, so
producers and readers must be upgraded together. The receipt envelope remains 1.0.

Checks are `producer_self_check`; no independent verifier is introduced. The
receipt's pass outcome follows the report's error-severity gate, so failed warning
checks remain visible without failing a successful run. Usage stays unknown.
The report validates exact result-reference, version, fingerprint and check
agreement. Tracker records additionally validate task, seed, estimator/version
and full spec-hash agreement. These consistency checks do not attest provenance.

Old report/tracker records remain readable with `receipt=None`; reads never invent
missing actor/trace/seed information or rewrite stored records. This adapter adds
no execution-resume support. Preparation, sandbox, persistence, tracker and audit
exceptions still propagate without returning a report/receipt. A completion audit
failure can occur after artifact/tracker persistence, as before; no transaction
spans those ports. Evaluation adapters and notebook replay remain unchanged.

The local sandbox fingerprint continues to mean worker bytes plus Python runtime;
it does not fingerprint all installed libraries or host preparation code. OCI
fingerprints retain their pinned-image meaning. Neither is promoted to proof of
full-environment reproducibility or production acceptance.

## Evaluation compatibility

Fresh evaluation runs may explicitly supply an operator-declared research actor
and receive per-step receipts with persisted same-task result artifacts. The
adapter preserves the legacy result serialization and hash, linking existing
step/path/method field bindings to those artifacts. It checks actual run inputs,
static database hashes and artifact bytes; active SQLite WAL is refused. Step
verification describes only producer readback/hash checks. Tokens and costs are
not inferred from whole-case totals. Declared identity is not authentication.

Legacy rows without adapter fields remain readable and unmodified. New metadata
must be consistent with trace and field bindings. Qualification of new rows also
requires artifact readback, including method and raw value agreement; absent
stores or corrupt bytes block qualification. Existing SQL dependency, method and
scoring checks remain in force. See the [evaluation receipt guide](../guides/evaluation-receipts.md).

## Alternatives considered

- **Extend `DeliveryPackage` with receipt fields.** Rejected: the package is a
  delivery shape, and failed executions that never deliver still need receipts.
- **Reuse the audit event as the receipt.** Rejected: audit events record policy
  decisions and are append-only evidence of what the gateway authorized, not a
  description of an execution's inputs, method and verification scope.
- **Keep per-consumer evidence shapes.** Rejected: it is the duplication that
  makes verification strength ambiguous today.

## Consequences

Consumers gain one place to ask "what ran and how strongly was it checked", at
the cost of a migration that must be done per capability. Rule adoption adds the
inline receipt described above; science embeds a receipt in its report. Evaluation
adoption is opt-in for fresh research runs; legacy records stay unchanged.
This ADR supports no quality or production claim.
