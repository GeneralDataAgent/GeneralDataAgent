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

This is a discovery-only example, not an enabled feature. The current rule supervisor has a
bound `resolve_rule_request` discovery tool rather than `search_documents`, and the benchmark
`BoundedSQLAgent` has no `specialist_skills` injection. Loading this JSON alone connects neither
entry point. Any future adapter must explicitly select and fingerprint the Skill/knowledge inputs,
use existing admission checks, and preserve the baseline configuration as a separate run.

`examples/payment_document_skill_demo.py` shows the local wiring path without a live model:
it loads this JSON, enrolls a synthetic custom specialist and the Skill through the normal
`CapabilityRegistry` propose/review/activate flow, and passes a real `search_documents`
`StructuredTool` built on `split_passages` / `select_passages` / `citation` into
`build_supervisor`. A scripted offline model delegates to the specialist and calls the tool, and
the test checks an exact source-line citation. The enrolled evidence hashes, reviewers, manual,
and model output are synthetic fixtures; the demo is not benchmark integration, live-model
evidence, or production acceptance, and it is not connected to any evaluation runner.

The current document facility is heading-based `split_passages` / `select_passages`, with exact
line-span citations. Start with versioned source files and these retrieval primitives; add an
index only when corpus size or retrieval evidence warrants it. A knowledge corpus is not an
experience store: the latter contains independently reviewed advisory repair patterns.

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
