# ADR 0010: Versioned execution receipt contract

Status: Proposed on 2026-10-26. The contract in
`src/generaldataagent/contracts/receipt.py` exists and is tested, but **no
capability emits a receipt yet**. This ADR is the contract review that ADR 0009
requires before adapting rule and science execution.

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
the cost of a migration that must be done per capability. Until adoption lands,
this ADR changes no runtime behaviour and supports no quality or production
claim.
