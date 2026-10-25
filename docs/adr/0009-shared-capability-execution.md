# ADR 0009: Shared capability execution across product and research

Status: Proposed on 2026-10-25 after reviewing the Stage 7–10 implementation and accumulated
milestone changes. This records the next implementation direction; the interfaces below are not
implemented or admitted for production by this ADR.

## Context

The repository already has useful general capabilities: exact public-task interpretation,
deterministic rule and science kernels, source-bound protected checkpoints, task-scoped artifacts,
reviewed experience, and a shared delivery contract. The fixed graphs and deterministic policy
boundaries should remain.

Reuse currently stops too low in the stack. `business/rules/workflow.py::AnnexWorkflowServices`
and `evaluation/stage_nine.py::analyze_fee_rules` both coordinate semantics, annex loading, facet
selection, merchant/month context, computation, and checks. They differ in formula acceptance,
source citations, decimal representation, and numeric-looking rule IDs. Presentation differences
belong in adapters; independent authoritative execution creates a continuing parity burden.

Execution controls also vary by composition. `ToolGateway` handles policy, audit, exact approvals,
and call budgets. The metric runtime enforces additional production constructor requirements.
The science workbench performs policy/audit itself, the evaluation agent directly dispatches local
capabilities, and rule supervision can invoke the development workflow without a governed runner.
These paths have explicit local/research boundaries; their existence does not establish production
source authorization or safe admission.

Evidence is fragmented across evaluator step dictionaries, answer binding receipts, rule state
checks, experiment reports, and task-bound artifact references. A supervisor role rereading a
verification result is not an independent semantic oracle. A content hash proves byte identity,
not origin, reviewer identity, independent execution, or causal benefit from experience.

## Decision

### 1. Converge rule application behavior first

Extract a typed application service over an immutable annex input, explicit semantics, and a
payments-source port. It owns context construction, selection, computation, and authoritative
checks. It must be usable without LangGraph, model access, or production infrastructure.

The fixed workflow calls that service stage by stage and retains checkpoint/binding checks.
The evaluator calls the same service through an adapter. Evaluation-specific ID conversion,
floating-point answer presentation, answer binding, and dataset import stay outside the service.
Define and test the intentional formula/citation differences before migration; do not silently
change historical benchmark protocols or relabel old outcomes.

Begin with one shared context/selection slice, then computation and checks. Preserve public imports
until both consumers migrate. Keep the raw-record/Fraction qualification oracle independently
implemented.

### 2. Make execution composition explicit

Introduce a small execution context and capability port carrying task/actor/trace identity,
source bindings, and configured control dependencies. Provide explicit local research and admitted
product compositions. A research adapter may intentionally use local read-only execution; it must
not acquire production status through a profile name such as `governed`.

Retain capability-specific production constructor checks. A shared context cannot substitute for
scoped source authorization, verified audit, governed artifacts, budgets, or protected checkpoints.
Select one consistent artifact-authorization boundary after tracing existing writers; do not add
redundant policy calls mechanically to every graph node.

Apply this first to rule execution. Then share experiment preparation/execution between the science
workbench and regression evaluator, including caller-supplied task/snapshot identity and sandbox
injection. Product execution must use the gateway's policy/audit/budget behavior.

### 3. Introduce versioned execution receipts

A proposed receipt records task/action identity, input/source digests, method/version/runtime,
result reference/hash, completeness, resource observations, and checks with declared verification
scope. Its exact schema requires a separate contract review before implementation.

Research sinks can retain bounded local results; product sinks persist governed artifacts. Answer
bindings should move from incidental step numbers to receipt identity plus result paths through
compatibility adapters. Producer checks, authoritative re-derivation, independent implementations,
and human attestations remain distinguishable. Missing usage stays unknown.

Add task-aware package validation for provenance and stored bytes. `DeliveryPackage` currently
validates reference membership and table consistency; the schema alone does not enforce same-task
provenance. Preserve existing runtime checks during migration.

### 4. Separate public semantics from evaluation transport

Keep public-text acceptance and answer requirements reusable. Move native/JSON wire normalization
and benchmark envelopes out of the semantic application layer, preserving existing imports during
migration. Extract an injected dispatcher only after the capability/receipt boundary has a second
consumer. Demonstrate it through a non-benchmark local analysis entry point before adding a broad
natural-language API.

### 5. Extract only demonstrated supervisor lifecycle duplication

After capability convergence, factor bounded team invocation, completion handling, timeout behavior,
and bound empty-argument tools shared by metric and rule supervisors. Keep their fixed delegation
orders, candidate handling, and domain validation local.

## Exit criteria

The first implementation milestone requires all of the following:

- Product workflow and evaluation adapter use one context/selection implementation, with explicit
  compatibility tests for all four rule operations and documented presentation differences.
- Deterministic fixtures cover a second synthetic annex, unseen identifiers, date boundaries,
  thresholds, missing source data, and decimal arithmetic; benchmark labels remain outside runtime
  inputs. Agreement between adapters is a parity check, not an independent correctness oracle.
- Existing protected resume/source-binding tests and independently implemented qualification
  checks remain effective. Changing source bindings prevents further computation/delivery.
- A non-benchmark local caller demonstrates the service without model access or evaluator imports.
- Full repository CI gates pass. Any changed evaluation protocol receives a new fingerprint;
  historical reports remain unchanged. Live quality/generalization claims require separately
  recorded complete runs, failures, denominators, resources, and independent review.

Subsequent receipt/composition milestones require cross-task reference rejection, missing control
rejection, audit outage rejection, and resource/verification scope preservation across adapters.
A local pass or a merged PR never implies production acceptance.

## Supporting consolidation

As these slices migrate, move extraction provenance contracts and pure validation below both
business storage and evaluation adapters. `ExperienceStore.capture_from_extraction` currently
imports the evaluator, which itself imports business experience contracts. Reuse evidence validation
and review-state reduction across reading, review, and audit while retaining scope authorization
and source qualification as separate checks. Declare schema/migration versions explicitly rather
than inferring legacy provenance from missing tables.

Likewise, share a validated run envelope below treatment comparison, repeat stability, and service
readiness; keep each consumer's purpose-specific blockers. Use a small immutable execution-budget
value derived from a frozen route to generate both worker payload and protocol declarations.
These are bounded foundations supporting the application migration, not separate frameworks.

## Alternatives and boundaries

Do not build a universal workflow engine, enterprise rules DSL, generic keyword router, or automatic
experience activation pipeline now. Keep fee predicates and natural-month rules domain-specific.
Keep deterministic graph control outside the model loop. Do not merge independent verification
code merely to reduce duplication.

Experience stays independently reviewed, scoped, bounded advisory input. Its current extractor
recognizes a narrow repair pattern; successful retry does not establish answer correctness or
causal reuse benefit. Immutable source snapshots, authenticated evidence, and paired unseen-task
runs remain separate work.

## Consequences

This prioritizes application-level reuse over further benchmark-bound edge-case expansion. It
reduces the number of authoritative behaviors to maintain while keeping research and production
controls explicit. It also creates migration cost: source/formula policy differences and evidence
versions must be resolved deliberately, and no existing production check may be weakened to make
research execution convenient.
