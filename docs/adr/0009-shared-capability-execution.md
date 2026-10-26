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

The metric and rule teams now share bounded invocation through
`agents/supervision.py::continue_role_sequence`. A graph that returns after a real tool stage
may receive a controller-owned next-role message on the same thread. Each additional invocation
requires new role progress, and at most one invocation per declared role is allowed. Repeating
the previous role, making no tool progress, a failed deterministic candidate, an explicit
`failed`/`needs_human` result, or a graph interrupt stops continuation. The existing total
deadline encloses every model invocation; provider errors propagate to the runtime's failure
result. No final model completion claim can replace the tool sequence or validated delivery.
The reporter remains a deterministic assembly step after the verifier passes, but an explicit
team stop prevents automatic reporting. `TeamStopMiddleware` intercepts the task tool's returned
structured stop before control reaches the next delegation, including within a single graph
invocation. The final reporting step is awaited within the same deadline and exception handling;
its audit/storage error yields failure rather than escaping the runtime. Cancellation of an
await does not forcibly terminate synchronous storage work already running in an executor. Synthetic provider scripts exercise the actual Deep
Agents graph, checkpoint reuse, policy audit, and both capability entry points. They demonstrate
control behavior, not a resolution of the historical live provider's stopping cause.
The rule supervisor runner fingerprints both the shared role controller and completion helper
so later runs declare this lifecycle change without rewriting historical reports.

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

## Dataset extension boundary

Dataset-specific vocabulary, public manuals, field dictionaries and procedures should first use
replaceable, versioned Skills and knowledge inputs through existing admission/retrieval interfaces.
Dataset import and answer presentation stay in adapters. Missing source semantics remain explicit
gaps or assumptions; expected answers are not runtime knowledge. Shared execution bugs, identity,
budgets, authorization and duplicated authoritative behavior are core implementation work.
See `docs/guides/dataset-extensions.md` for the current integration limits and example Skill.

The first implemented slice now shares facet selection through
`business/rules/application.py::select_rule_facets` in both adapters, with a non-benchmark local
example. Context construction, computation and execution composition remain migration work; this
slice does not complete the proposed service or establish live quality/generalization gains.

The second implemented slice adds `orchestration/composition.py::ExecutionComposition`, an
immutable value that binds one task's actor, artifact store, audit sink and optional budget under
either a `research` or a `product` mode. `product` refuses at construction unless the ports are a
`GovernedArtifactStore`, a `VerifiedAuditSink` and a `TaskBudgetLedger`; `research` admits the local
development ports. The rule entry point consumes it: `GovernedRuleRunner` and `RuleDeliveryRuntime`
accept an optional composition, check that the wired ports are the composition's own, and switch
the gateway's `production_mode` and budget ledger from it, so a product run is refused when the
audit service is down or the task budget was never registered. A composition is a statement about
wiring, not an authorization: it does not admit an actor, a release or a data source, and its
`production_ready` property reports wiring completeness only.

The same composition now reaches the other capabilities. `DataScienceWorkbench` runs experiments
under `prepare_experiment`/`run_prepared_experiment`, which the regression evaluator also uses, so a
preparation control added once reaches both; `MetricAnalysisRuntime` is the last entry point to
accept a composition. Artifact authorization has one boundary (`require_artifact_scope`) instead of
each writer re-deriving the tenant/project/domain comparison. A composition may carry `task_id` and
`trace_id`; when it does, the rule runner, rule delivery, science workbench and metric runtime use
them rather than minting their own, and each refuses work outside the composed task. Metric analysis
under a composition no longer registers a default budget, so an uncomposed caller keeps today's
behaviour while a composed one must have its task registered first — and a composition that carries
a ledger without a `task_id` is refused when a runtime is built from it, because a ledger that
meters per-run minted tasks would silently authorize usage the operator never composed.

A composition must be the whole of a runtime's control wiring, not an addendum to it. The rule
delivery runtime took a composition and still defaulted to no audit sink, which would have
suppressed policy audit for every decision it made; it now adopts the composition's sink instead of
requiring callers to pass it twice. Metric analysis `resume` checked that the thread belonged to the
composed task but not that the caller was the composed actor, so a runtime composed for one actor
could finish another actor's thread and record it through its own ports; the actor check now runs
with the task check. Both were reproduced before being fixed and are pinned by regression tests.

Still open in this milestone: the receipt milestone.

`ExecutionIdentity` (in the same module) now owns the two questions every consumer was asking
separately: whether a caller's actor is inside the bound scope, and whether its task is the composed
one. The two answers are deliberately different checks — `require_covering_actor` admits a narrower
actor (same user, tenant and project, domains a subset), `require_equal_actor` requires the same
principal — because science compares a partial actor while rule delivery requires the identical one.
Naming them made an existing divergence explicit instead of leaving each caller to inherit whichever
rule its neighbour happened to encode. `resolve_task`/`resolve_trace` carry the "composed value wins"
rule, and the composition validates through it while keeping its public failure codes.

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

The dependency direction is now part of this consolidation rather than an accident of history.
Three runtime ports lived under `evaluation/`, so importing them pulled the eager package `__init__`
(stages one through three) into API startup: declared-key introspection moved to
`catalog/schema_relationships.py`, the human review stores to `generaldataagent.review`, and the
action-qualification contract to `security/action_qualification.py`, each leaving a re-export where a
pinned evaluation path named it. Two facts made this worth doing beyond import cost: `evaluation`
is offline tooling, so a runtime module importing it is a layering inversion whatever the timing,
and the same file had been assigning `__all__` twice, silently discarding the stage-one/two/review
exports. Both directions are now asserted by tests that run in a fresh interpreter.

Moving those ports was not enough on its own: `orchestration/__init__.py` still imported
`workflow` (LangGraph) and `metric_analysis` (the LLM stack) eagerly, so any module importing
`orchestration.composition` — the science workbench, for one — paid for both. Importing
`generaldataagent.evaluation` went from 146 ms with no LangChain loaded to 1102 ms with it loaded,
which is a regression the offline acceptance tooling would have carried. Only the composition
values are eager now; the rest resolve through a caching `__getattr__`. A test imports the offline
package in a fresh interpreter and fails if any `langchain`/`langgraph` module appears. The
complementary direction is asserted by parsing every runtime module and rejecting a
module-level `generaldataagent.evaluation` import: a function-local import is the sanctioned form
(`business/experience.py` reaches one extractor that way), and moving it to the top of the file
would put the whole offline package back on the API's import path.

A later adversarial review of the milestone found four defects worth fixing and one claim that
did not hold. `RuleDeliveryRuntime` accepted a composition but kept its own `audit=None` default,
so policy decisions for that runtime were unaudited even though the composition declared a sink;
it now adopts the composition's sink. `MetricAnalysisRuntime.resume` checked the composed task but
not the composed actor, so a runtime composed for one actor could finish another actor's thread and
write it through its own ports; `require_equal_actor` now runs alongside the task check. A
composition carrying a budget ledger but no `task_id` was accepted, which would have metered each
run against a freshly minted default task — exactly what the composition exists to prevent — and is
now refused at construction. The review also reported a `user_id` regression in the science
workbench actor check; `git show` of the pre-slice file shows the inline check never compared
`user_id` either, so it is pre-existing rather than introduced, and it is recorded below as an open
question rather than folded into this milestone's fix.

Open, deliberately not changed: `GovernedArtifactStore._authorized_record` compares tenant,
project and domains but not `user_id`. Widening it is a policy decision, not a bug fix, because the
store is described everywhere as tenant/project/domain-bound, and `business/report_refresh.py`
already applies a stricter owner check on the same read path. Until that decision is made, artifact
reads are scoped to the tenant/project/domain, and per-user artifact confidentiality rests on the
callers that add the owner check.

## Admitted advisory document consumer

The rule Supervisor now accepts an optional `RuleDocumentExtension` carrying a supplied
`DocumentContext`, an `ExtensionAdmission`, and a reviewed discovery Skill. It requires the
existing governed runner for the same workflow. The Skill retains exactly bound discovery and
document search; it cannot expand the other specialists' tools. Admission is checked while
assembling the graph and again before every search. Search is allowed only during the active
discovery stage, through `documents.search` policy/audit/budgets, followed by an audited task-bound
quotation artifact write. Audit failure prevents the search handler from running.

This context is advisory. Documents do not alter the approved request, annex semantics, or
workflow source binding. Missing evidence and contradictory quotations remain explicit results;
this slice does not implement semantic conflict adjudication or dependency-based recomputation.
The runner enables it only through an explicit pinned configuration with pre-existing admission,
checks exact document bytes and canonical Skill hash before provider construction, and records
configuration/Skill/corpus/runtime fingerprints. Disabled runs remain a separate treatment. An
unadmitted example Skill is supplied; no activation, real-model gain or production qualification
is implied by shipping it.

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
