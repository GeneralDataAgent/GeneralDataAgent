# Dataset extensions and core capability changes

Dataset-specific conventions belong in replaceable, versioned Skills and knowledge inputs.
Dataset-independent execution defects and shared design problems belong in core implementation.
A development checkpoint does not need to wait for quality or production acceptance; preserve
those statuses separately.

## Choosing the change boundary

Use a knowledge input for public manuals, field dictionaries, units, domain vocabulary, and cited
business conventions. Use a Skill for a repeatable procedure: which documents to consult, what
context to assemble, what to verify, and how to report missing evidence. Keep dataset import and
answer wire formatting in evaluation adapters.

Fix core code for incorrect arithmetic, incomplete collection, lost task/source identity,
authorization failures, lifecycle/budget bugs, or duplicated authoritative application behavior.
If a failure mixes missing domain knowledge with an execution bug, split the changes and record
both causes. Do not add dataset names or question IDs to shared algorithms.

## Existing integration points

`examples/skills/payment-document-context.json` is an unadmitted `SkillDefinition` example. It
narrows a selected specialist to an existing `search_documents` tool; it cannot add that tool.
Load it with `SkillDefinition.model_validate_json`, propose/review/activate its exact hash through
`CapabilityRegistry`, then pass it to `build_supervisor` using `specialist_skills`, `skill_context`,
`actor`, and `admission`. The chosen specialist must already own the declared tools. Provide
`question` and bounded `documents` as literal context. Changes require a new reviewed hash.

This JSON remains a discovery-only example. The rule supervisor now accepts an explicit
`RuleDocumentExtension(context, admission, skill)` and requires a `GovernedRuleRunner` using the
same workflow. Its reviewed discovery Skill must retain exactly `resolve_rule_request` and
`search_documents`, with `bound_request` and `document_manifest` available as literal template
inputs. The example JSON does not meet that combined tool contract unchanged. Search is available
only after bound discovery and before analysis; each search rechecks admission, runs through
`documents.search` gateway policy/audit/budgets, and persists a task-bound quotation artifact.
Missing or contradictory text remains advisory evidence and cannot rewrite the bound request or
approved rule semantics. Disabled configuration adds no document tool. The benchmark
`BoundedSQLAgent` has a separate explicit library Skill port, described below.

`examples/skills/rule-document-discovery.json` supplies an unadmitted combined discovery Skill
matching this contract. Propose, review and activate its canonical `ExtensionAdmission.skill_hash`
for the intended actor before use. Its examples and procedure are guidance, not acceptance evidence.

`evals/run_stage10_rule_supervisor.py --document-extension <config.json>` selects this treatment
explicitly; omit it for a separately recorded disabled run. The JSON configuration requires
`registry_path`, `skill_path`, `skill_hash`, and `documents`, with optional
`verification_adapter` and `skill_dependencies`. Each document entry has `path`,
`source`, `version`, and `content_hash`. Paths resolve from the implementation repository and must
stay inside it. Both document hashes (exact UTF-8 bytes) and the canonical Skill hash are pinned
and checked before provider construction. The registry must already contain an active reviewed
Skill for the evaluation actor; the runner never enrolls or activates it. Reports retain enabled
state, configuration hash, Skill identity/hash, corpus manifest and runtime hashes. No live run
or quality improvement is implied by this integration.

For a root with declared prerequisites, provide the complete definitions explicitly:

```json
{
  "skill_dependencies": [
    {"path": "skills/citation-policy.json", "skill_hash": "sha256:<canonical-skill-digest>"}
  ]
}
```

This is a fragment to add to the required configuration. The dependency list defaults to empty;
explicit null, duplicate names/versions, unknown entry keys, outside paths and more than 64 entries
fail. Each Skill file has the same 128,000-byte limit and canonical hash pin as the root. The complete
closure passes the bounded assembly rules below before provider construction, using an empty request
placeholder plus the real corpus manifest; every actual request is rendered and checked again.
No registry records are created or activated. Missing or revoked dependencies refuse the treatment.

In Python, pass `skill_dependencies={"citation-policy@v1": citation_skill}` to
`RuleDocumentExtension`. It copies that mapping into an immutable snapshot and passes the definitions
to the Supervisor factory. All reachable references are checked during graph use, before and after
governed search, and after the optional document check. Extension summaries include the supplied
dependency identities/hashes when nonempty; omitting dependencies preserves the old summary shape.
Runner reports also retain `skill_admission_hash` and `skill_bundle_hash`. The document verifier
remains explicitly bound to the root Skill and verifies retrieval structure; dependency verification
prose is not executed or independently accepted.

### Optional executable document check

Set `"verification_adapter": "document-evidence-v1"` to enable the repository-owned
`DocumentEvidenceVerifier`. In Python, construct it with the admitted Skill's name, version and
canonical content hash, and pass it as `RuleDocumentExtension(..., verifier=verifier)`. The binding
must match the exact Skill revision; unknown adapter identifiers and explicit null values fail.
Omitting this option retains the existing advisory treatment and extension summary.

The adapter re-derives lexical retrieval using the same repository implementation and compares
canonical JSON bytes for the entire result: query, corpus manifest, source versions/hashes,
quotations, headings, line spans, citations, passage hashes, gap, hit count and truncation. It also
checks the evidence artifact's task/type and exact readback body. Admission is checked before
search and again before releasing the checked response. A mismatch aborts execution; a legitimate
retrieval gap or conflicting quotations remain advisory and may coexist with a valid rule result.
Skill `verification` and `failure_handling` prose does not select code or define failure policy.

The returned `structural_verification` field records adapter identity, exact Skill binding,
`producer_self_check` scope and check codes. It is tool-response metadata; it is not added to the
stored retrieval artifact and is not an independently signed or durable verification receipt.
The runner records `document_verifier_hash` alongside other source hashes. This check establishes
consistency with the supplied snapshot, not source authenticity, business interpretation,
independent implementation, benchmark improvement or production acceptance.

`examples/payment_document_skill_demo.py` shows the local wiring path without a live model:
it loads this JSON, enrolls a synthetic custom specialist and the Skill through the normal
`CapabilityRegistry` propose/review/activate flow, and passes a real `search_documents`
`StructuredTool` built on `split_passages` / `select_passages` / `citation` into
`build_supervisor`. A scripted offline model delegates to the specialist and calls the tool, and
the test checks an exact source-line citation. The enrolled evidence hashes, reviewers, manual,
and model output are synthetic fixtures; the demo is not benchmark integration, live-model
evidence, or production acceptance, and it is not connected to any evaluation runner.

`business/rules/document_context.py` now wraps the heading-based primitives as a reusable
explicit-input port. Construct `MarkdownDocument(source=..., version=..., text=...)` for each
supplied revision and pass the sequence to `DocumentContext`, or to `build_search_documents` for
the tool adapter. Matches carry source/version, the exact UTF-8 input hash, a passage hash and
original line spans. Each source is unique within a snapshot; replacing text requires a new
snapshot. Missing documents/content/matches have distinct `gap` codes. Multiple contradictory
passages remain untrusted quotations; the port does not select a business interpretation.

Input size, result count and returned text are bounded. Count truncation is explicit through
`hit_count` and `truncated`; text overflow is refused rather than clipping a quotation. Hashes
identify supplied content, not its authenticity or approval. The tool adapter does not grant
permissions or register itself in the gateway: admission and execution controls remain with its
caller. The example keeps its single-document function signature over this shared port.

Start with versioned source files and this port; add an index only when corpus size or retrieval
evidence warrants it. A knowledge corpus is not an experience store: the latter contains
independently reviewed advisory repair patterns. The rule supervisor requires explicit admitted
configuration; the benchmark SQL agent does not enable this port.

## Bounded Skill dependency assembly

`ExtensionAdmission.render_skill_bundle` resolves the root Skill's declared `name@version`
prerequisites from an explicit mapping of those names to complete `SkillDefinition` objects.
Pass the same mapping as `build_supervisor(..., skill_dependencies=definitions)` when assigning
root Skills through `specialist_skills`. No definition is downloaded, inferred from a registry
digest, enrolled or activated by this resolver. Each reachable definition needs its own current
admission for the exact content hash, actor scope, name and version.

```python
text, references = admission.render_skill_bundle(
    root_skill,
    actor,
    literal_context,
    dependencies={"citation-policy@v1": citation_skill},
    task_budget=task_budget,
)
```

The closure is rendered in declared dependency order before the root; shared prerequisites appear
once. Every edge must narrow its parent's tool list, required domains and declared call/token/time
ceilings. A root therefore declares all transitive tool/domain needs. Each definition also passes
the existing input, DLP, actor-domain and task-budget checks using one copied literal context.
Budget checks compare declarations; they do not allocate additive sub-budgets or introduce a new
per-Skill usage meter. Existing task/gateway/model controls still enforce actual work.

The supplied cache is limited to 64 definitions, each closure to 32 Skills including the root,
depth to 8 including the root, and combined rendered text to 16,000 characters. Missing definitions,
cycles, two versions of one Skill name, key/content mismatches and wider child requirements fail
during assembly. The factory also rejects root tools absent from the selected specialist. Unused valid cache entries are not rendered or admitted.
The factory retains every reachable pinned reference in `AdmittedGraph`, which rechecks admission
before and after invocation and during streams. Admission is also rechecked at the end of rendering.

Without dependencies the rendered text, Skill hashes and reference format stay unchanged. The
legacy single-Skill `render_skill` API now refuses a nonempty dependency declaration with
`skill_dependencies_require_bundle` instead of silently ignoring it. A changed dependency or list
requires the normal reviewed versioning flow; existing records are not rewritten.

This is instruction assembly, not a generic Skill execution language or a semantic conflict solver.
Verification/failure prose does not run as code. The rule document extension accepts explicit pinned
bundles through the opt-in configuration above; the bounded agent uses the separate library port below.
No tool authority, production admission or quality gain
follows from composing instructions.

Run the synthetic real-graph wiring example with:

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  uv run --frozen python examples/payment_document_skill_demo.py --with-dependencies
```

It enrolls a synthetic citation prerequisite as well as the root Skill, then uses the existing
scripted model and real document search. Both procedures reach the specialist, every reference is
pinned, and a revoked dependency blocks reuse. The example is fixture setup and wiring evidence,
not live model, independent acceptance or production evidence.

## Bounded agent library Skill port

`BoundedSQLAgent(..., skill_extension=BoundedAgentSkill(admission, actor, skill, dependencies))`
accepts an explicitly admitted procedure and prerequisite cache. Import `BoundedAgentSkill` from
`generaldataagent.evaluation.skill_extension`. There is no automatic enrollment, discovery or CLI
configuration, and `evals/run_stage6.py`/its worker do not enable this port. A library integration
does not constitute a benchmark treatment or an evaluated improvement.

This port's `allowed_tools` vocabulary is the agent's canonical dispatch keys, such as `sql`,
`search`, `analysis`, `answers` and `final_sql`, rather than native function aliases or Supervisor
tool names. The root must be a subset of its selected profile's capabilities. Dependencies use the
same monotone bundle rules above. Native schemas are filtered to this surface, and normalized JSON
and native actions are checked before dispatch, including planning and final submission actions.
`unsupported` remains a controller-owned exit even if the Skill omits it. It grants no data access.

```python
from generaldataagent.evaluation.skill_extension import BoundedAgentSkill
from generaldataagent.evaluation.stage_six import BoundedSQLAgent

procedure = BoundedAgentSkill(admission, actor, root_skill, dependency_definitions)
agent = BoundedSQLAgent(model, tool_profile="sql-analysis", skill_extension=procedure)
result = agent.run(public_task, database, documents=public_documents)
```

The literal template context contains only `question`, `instructions` and `document_outline`
(bounded document headings). It receives no labels, reference SQL, task identifier or host path.
Definitions are copied into an immutable mapping. Assembly, exact-hash admission, actor-domain,
declared-budget and literal/DLP checks happen before database inspection or model calls. The
rendered instructions and controller action ceiling are added after profile-specific prompt
selection, so profiles retain them.
Actual document access continues through the existing bounded search implementation; its excerpts
are advisory, and this port does not add the rule consumer's line citations or structural verifier.

All reachable references are rechecked before each model invocation, after response usage is
accounted, before dispatch, after tool execution and before releasing the final result. Missing
admission or a disallowed action terminates with `skill_admission_refused` or
`skill_action_not_allowed`, clears answer/SQL submission fields and spends no repair opportunity.
Already observed tokens/cost remain recorded. Preflight configuration errors raise before any
model call. No synchronous query or provider operation can be forcibly cancelled by this admission
check; revocation prevents releasing its result after it returns.

Enabled results include pinned references, allowed actions, rendered-text hash and the count of
returned model responses in `skill_extension`. A returned response confirms that invocation received
the prompt; failed calls are not counted as confirmed delivery. With no extension, the prompt and
result shape remain unchanged. The runner fingerprints the new implementation file; historical
reports are not rewritten. Budget checks compare Skill declarations to existing task ceilings;
they do not introduce per-Skill consumption meters or automatically lower the task's configured
limits. Verification/failure prose remains declarative. This development agent is still not a
production-authorized database gateway or independent acceptance path.

## First knowledge inputs for payment analysis

The downloaded public development assets include `data/stage6/dabstep/manual.md` and
`payments-readme.md` (ignored data files, not shipped documentation). A reproducible knowledge
bundle should bind their original bytes and source manifest, retain source names/line spans, and
separate source excerpts from curated interpretation. Do not copy expected answers or reference
SQL into the bundle.

Useful source locations in the currently inspected files:

- Manual lines 23–36 and 55–69: account types and ACI vocabulary.
- Manual lines 74–97: merchant/transaction facets, fee formula, natural months, and null behavior.
- Payments dictionary lines 9–14, 16, 23 and 26: date fields, amount, issuer, fraud flag and acquirer.

Line locations are navigation aids; rebind them against the actual file bytes when constructing a
bundle. The manual states null-as-wildcard but does not define empty-list behavior. Its existence
in a corpus does not justify silently changing `empty_list_is_wildcard`. Record any chosen reading
as an explicit assumption with its own provenance. Knowledge availability alone does not establish
that a model retrieved, understood, or used it.

## Development evidence

Check extensions with synthetic positive/negative examples, missing and conflicting documents,
permission narrowing, exact-version admission, and source replacement. For core changes, use
independent expected results and a second synthetic dataset, plus the existing workflow/evaluation
regressions. Keep independently implemented qualification oracles independent of shared services.

For live comparisons, declare Skill and corpus versions/hashes in both run configurations, retain
all cases/failures/resources, and report the extension treatment separately from core changes.
No new live evaluation is implied by adding an example Skill or refactoring a shared function.
