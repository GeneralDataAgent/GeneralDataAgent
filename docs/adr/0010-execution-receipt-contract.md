# ADR 0010: Versioned execution receipt contract

Status: Proposed on 2026-10-26. The contract in
`src/generaldataagent/contracts/receipt.py` exists and is tested.
`GovernedRuleRunner.invoke()` now returns a receipt for persisted workflow
evidence. Science execution and evaluation compatibility remain pending. This
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
  `runtime_fingerprint`
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
- Not a compatibility shim yet: mapping evaluation step bindings to receipts is a
  later slice with its own tests.

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
`RuleDeliveryRuntime` and `DeliveryPackage` retain their existing interfaces.

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

No old evaluation reports are rewritten. This slice does not aggregate receipts
into the Supervisor's result or the delivery package, and does not add a science
adapter or independent-verifier claim.

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
inline receipt described above; science and evaluation adoption remain pending.
This ADR supports no quality or production claim.
