# Stage 5 business-loop development evidence

Updated: 2026-10-19 (Asia/Shanghai). Branch: `feat/stage5-business-loop`.

## Delivered repository scope

| Roadmap delivery | Implementation | Boundary |
|---|---|---|
| Dashboard, ticket, scheduling PR, registration, IM and report actions | Six discriminated strict payloads, authenticated draft/status/execute/cancel API, persisted task/thread and exact payload | Local drafts for all six kinds; explicit Feishu notification receiver now supports real send/readback. Other real receivers and production deployment remain unavailable |
| Idempotency, approval, expiry, cancellation | Durable actor-scoped key/content conflict detection, exact one-time approval, separate requester/reviewer, replay receipt | Interrupted consumed/claimed actions quarantine for reconciliation |
| Compensation/rollback | Separately approved compensation referencing original content and current revision | Retracts the matching local resource only; cannot overwrite a later revision |
| Execution guards | Current session, scope, DLP, policy, destination revision, per-kind owner/switch/rate and global switch | Controls are trusted configuration; no approval/control mutation HTTP endpoint |
| Delivery evidence and audit | Intent/payload/receipt hashes, transactional resource/receipt/completion and chained local events | Independent external audit/retention and receiver reconciliation still required |
| Periodic reports | Durable finite schedule, bounded tick, stable occurrence IDs, independent approval per occurrence, skip expired periods | Optional certified query refresh with fixed or rolling windows, immutable generated artifact and per-occurrence approval; external publication and autonomous service identity remain separate work |
| Cost/latency, routing/cache | Opt-in async admitted-model executor, bounded tokens/cost/deadline, live authorization checks, deterministic configured-cost route, encrypted 15-minute-max scoped cache | Integrated into the analysis narration node reached by Supervisor tools; orchestration model remains separately configured. Operator/provider enrollment and measured billing qualification are still required |
| User feedback/evaluation governance | Completed metric deliveries register scoped hashes; authenticated feedback, DLP, two independent reviews, immutable datasets and task/evidence holdout separation | Local trusted reviewer ports; no automatic training or production review service |
| Agent/Skill/connector admission | Pinned immutable version/evidence/owner/scope/expiry/rollback, independent quality/security reviewers, activation and permanent revocation | Enforced in specialist/Skill factory and connector assembly/native reads, with live revocation checks. No dynamic imports or automatic tool-permission expansion |
| Independent per-action acceptance | Signed, revision/configuration/environment-bound external receiver measurements, safety/compensation/audit/business targets, separate result for every action kind | Missing evidence stays `not_run`/incomplete; no fixture can establish a production claim |

## Initial validation (2026-10-18)

- Full frozen suite: **333 passed, 1 skipped**, **87.28%** coverage (85% required).
- The skip is the opt-in live PostgreSQL check without its local test environment. CI supplies a
  disposable PostgreSQL service; local results do not imply a real production database qualification.
- Fifth-phase focused suite: **40 passed**, including exact approvals, concurrent retries, restart,
  compensation versus newer revisions, policy/domain changes, revocation, DLP, cancellation/expiry,
  uncertain claims, a kill switch between consumption and commit, API permissions, schedules,
  curation/holdout protection, admission/revocation, routing/cache/deadline and signed external gates.
- Frozen Stage 5 matrix: **42/42** (six action kinds × seven lifecycle scenarios), per-case records and
  dataset SHA-256 retained in runner output.
- Frozen Stage 4 security matrix: **91/91**; existing safety baseline remains passing.
- Ruff lint/format, lock consistency, whitespace checks and offline sdist/wheel builds passed.

Commands:

```sh
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen ruff format --check .
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache \
  uv run --frozen pytest --cov=generaldataagent --cov-fail-under=85
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage5_acceptance.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage4_acceptance.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv lock --check --offline
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv build --offline
```

CI now triggers on `feat/stage5-*` pushes and runs the Stage 5 matrix alongside the existing
Python 3.11–3.13 lint/test/security/PostgreSQL checks.

## External qualification

Each real action needs at least 20 receiver-side trials, zero duplicate effects/content mismatches/
security regressions, complete audit records, successful compensation drills, current identity and
policy checks, cancellation/expiry, independent switches/rate limits, crash reconciliation, native
authorization and verified receipts. Business metrics carry a named baseline, observed result,
improvement direction and a positive predeclared target.

The external bundle signs all measurements, evidence hash, target/owner, environment, exact code
revision and configuration hash. It expires within one day and requires distinct security and
operations identities with distinct trusted Ed25519 keys, neither equal to the owner. Trust is
operator-owned; removing a key blocks it on revalidation. Store raw receiver transcripts separately
and have independent reviewers verify the declared underlying evidence hash before signing.

Supply `<kind>.json` bundles with `--action-evidence-dir`, the existing trusted signer JSON with
`--trusted-signers`, and an operator-owned kind-to-SHA-256 mapping with `--configuration-hashes`.
`--environment` binds the target environment. No signing key or fabricated real-world transcript
is bundled. Tests generate ephemeral keys for explicit synthetic fixtures only.

The aggregate runner now also consumes signed optimization evidence and the independently signed
Stage 4 release bundle. It computes acceptance from all action gates, pinned held-out cost/latency
comparison, inherited release verification and a clean committed source tree. It can pass only when
all those verified inputs pass; `--require-production` exits according to the actual blockers.
The real environment remains **incomplete** because those independent production inputs are absent.
No synthetic test signature or the single connectivity smoke below supplies that missing evidence.

## Known limitations

Local state requires protected filesystem access and backups; chain verification cannot detect a
complete rollback without an independently stored checkpoint. Draft evidence hashes identify the
proposed references; a local draft receipt does not verify that an external system fetched or
accepted those sources. Hash/existence/authorization validation against real artifacts belongs in
the qualified receiver adapter. The remaining receiver mappings, production retention/legal holds, independently authenticated
approval services and provider tokenizer/billing qualification remain external integration work.
Report refresh and Feishu notification credentials/readback are now connected in the development pilot. Prices and latency estimates are trusted inputs, not measurements.


## Continuation validation (2026-10-19)

- Full frozen suite: **355 passed, 1 skipped**, **87.47%** coverage. The optional local PostgreSQL
  environment is the single skip; CI supplies its disposable native database service.
- Stage 4 security matrix remains **91/91**; Stage 5 action matrix remains **42/42**.
- Ruff lint/format, lock consistency, whitespace checks and offline source/wheel builds pass.
- Normal certified analysis now uses the optional metered model executor for narration. Fresh SQL
  and source authorization precede cache lookup; changed values/query/metric definition or current
  authorization invalidate reuse. Usage/audit and task-bound narrative lineage are preserved.
- The LangChain provider adapter forces a bounded Narrative tool response and requires actual usage.
  A test against the installed ChatAnthropic runnable proves the output-token bound reaches the
  model call, avoiding dropped kwargs in the prior raw structured-output pipeline.
- Schedules can execute a certified query per occurrence, including rolling windows, freeze a full
  evidence-backed report, and create a separately approved draft. Generated content survives a
  proposal failure unchanged. Cancellation during generation and modified evidence fail closed.
- New/changed specialists, attached Skills and new connector implementations cannot enter the normal
  factories without their pinned admissions. Revocation and changed implementation/configuration
  block use; a Skill may narrow available tools but cannot add new authority.
- Signed optimization and Stage 4 release evidence are now consumed by the aggregate runner. Tests
  use explicitly synthetic ephemeral keys to prove complete inputs can pass and omitted/tampered/
  expired/below-target inputs fail. No real acceptance signatures are generated.
- The user explicitly approved one bot-to-self Feishu test message. The actual message was sent,
  retrieved and verified for sender/conversation/exact content. Repeating the completed action
  returned the same platform message ID and revision without a second send. Runtime IDs/state stay
  in ignored protected local storage. No group message, recall or GitHub PR was performed.

The live Feishu pilot proves connectivity for one approved destination and message. It does not
prove the required 20-trial independent receiver qualification, real recall success, a measured
business improvement, complete production audit/retention or prior-stage production admission.
See the current [remaining-work list](../stage5-remaining-work.md) for concrete implementation and
operator dependencies, and the [runbook](../operations/stage5-runbook.md) for configuration details.
