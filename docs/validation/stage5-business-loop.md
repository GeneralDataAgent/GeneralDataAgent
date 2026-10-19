# Stage 5 business-loop development evidence

Updated: 2026-10-19 (Asia/Shanghai). Branch: `feat/stage5-business-loop`.

## Delivered repository scope

| Roadmap delivery | Implementation | Boundary |
|---|---|---|
| Dashboard, ticket, scheduling PR, registration, IM and report actions | Six discriminated strict payloads, authenticated draft/status/execute/cancel API, persisted task/thread and exact payload | Local drafts for all six kinds; explicit Feishu notification and bounded frozen-report IM receivers support send/readback; a single-file JSON scheduling GitHub draft-PR receiver supports approved creation/readback/closure. Only the notification smoke has been exercised live. Other receiver types and production deployment remain unavailable |
| Idempotency, approval, expiry, cancellation | Durable actor-scoped key/content conflict detection, exact one-time approval, separate requester/reviewer, replay receipt | Interrupted consumed/claimed actions quarantine for reconciliation |
| Compensation/rollback | Separately approved compensation referencing original content and current revision | Retracts the matching local resource only; cannot overwrite a later revision |
| Execution guards | Current session, scope, DLP, policy, destination revision, per-kind owner/switch/rate and global switch | Controls are trusted configuration; no approval/control mutation HTTP endpoint |
| Delivery evidence and audit | Intent/payload/receipt hashes, transactional resource/receipt/completion and chained local events | Independent external audit/retention and receiver reconciliation still required |
| Periodic reports | Durable finite schedule, bounded tick, stable occurrence IDs, independent approval per occurrence, skip expired periods | Optional certified query refresh with fixed or rolling windows, immutable generated artifact and per-occurrence approval; a durable worker supports fresh scoped sessions and restart-safe leases; deployment identity and external publication remain operator work |
| Cost/latency, routing/cache | Opt-in async admitted-model executor, bounded tokens/cost/deadline, live authorization checks, deterministic configured-cost route, encrypted 15-minute-max scoped cache | Integrated into the analysis narration node reached by Supervisor tools; native Supervisor/specialist routing now shares durable orchestration budgets and per-turn usage audit. Operator/provider enrollment and measured billing qualification are still required |
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


## Background execution and orchestration continuation (2026-10-19)

Implementation commits: `b4af8a0` (background worker), `fe957b4` (native orchestration routing),
`58ab49b` (missed-period revision recovery). No new real messages or model requests were sent.

- Full frozen suite: **381 passed, 1 skipped**, **87.74%** coverage. The single skip remains the
  optional live local PostgreSQL environment; it is not an unimplemented report/model feature.
- Stage 4 security matrix: **91/91**. Stage 5 local lifecycle matrix: **42/42**.
- Durable report worker: exact schedule/configuration/scope binding, finite delegation expiry,
  fresh least-privilege sessions revoked after every tick, no persisted bearer tokens, concurrent
  poller exclusion, expired lease recovery, sanitized failures and cancellation. Deployment needs
  a workload identity issuer and process manager; the tests do not establish either live service.
- Native model governance: real installed Deep Agents graphs with offline Anthropic/OpenAI
  provider responses exercise a Supervisor → discovery specialist → Supervisor round trip. All
  three turns share one durable budget and each native cap reaches the provider call boundary.
  SDK retries, streaming, native/global response caches and automatic unmetered summarization are disabled. No response/tool-call
  cache or automatic route fallback is used. Scope changes, revoked models, missing usage, token
  overruns, DLP, unsupported multimedia, deadlines and retry/concurrency budgets are exercised.
- Optional current-at-proposal report revision policy supports skipped/cancelled periods. Lost
  acknowledgment replay preserves the original intent/version/approval, and execution still blocks
  intervening resource changes. Existing sequential scheduling remains the default.
- Independent production gates remain incomplete. These tests are local engineering evidence;
  real reviewer enrollment, held-out provider billing/latency comparison, receiver targets and
  deployment/approval/audit infrastructure remain necessary. See the remaining-work inventory.


## Report publication and service-identity continuation (2026-10-19)

Implementation commits: `4694b46` (Feishu frozen-report IM publication) and `cf5c0dd`
(schedule-bound service JWT identity). These changes did not send any new real message or recall.

- Full frozen suite: **416 passed, 1 skipped**, **87.92%** coverage. The skip is the optional local
  PostgreSQL environment; remote CI supplies its disposable native PostgreSQL service.
- Stage 4 security baseline remains **91/91**, Stage 5 local lifecycle baseline remains **42/42**.
- Frozen-report publication has **14 isolated cases**: real local query/report generation followed
  by an exact full-text approval, fake receiver send/readback, durable receipt replay, lost-ack
  quarantine, read-only reconciliation, separately approved and confirmed recall, source/content/
  scope/classification/lineage/task/schema/size/mention checks and last-moment preflight changes.
  Report bytes are read under destination data-domain scope even when the owner has broader access.
- Service identity has **21 isolated cases**: ephemeral test RSA-signed service JWTs, exact audience
  and issuer/key constraints, short credential/session TTLs, mounted credential rotation, fresh
  report generation and Feishu report preview under a scoped service session, cross-process grant
  revocation, cancellation, permanent session invalidation, binding/job/control-scope tampering,
  and revocation during generation. Token actor/permission claims cannot grant authority.
- Service sessions can only propose exact enrolled schedule occurrences. Other owned schedules,
  unrelated drafts/cancellation, feedback and delivery actions are denied in runtime/API paths.
  An ordinary broker fails closed on service records rather than losing their constraints.
- Ruff lint/format, lock consistency, whitespace and offline source/wheel build checks pass.
- These are engineering/integration tests with local synthetic data and isolated receivers/keys.
  Actual report sending, real identity-provider enrollment/deployment, independent reviewer and
  receiver qualification, cost/business comparisons and production admission remain separate.


## GitHub scheduling draft-PR continuation (2026-10-19)

- Full frozen suite: **453 passed, 1 skipped**, **87.84%** coverage (85% required). The skip remains
  the opt-in local PostgreSQL environment.
- GitHub receiver: **37 isolated cases**, including exact before/after approval, pinned repository/actor/
  base identity, one-file content readback, default-branch preservation, idempotent replay after base
  advance, occupied branch refusal, concurrent execution, mid-operation revocation, deadlines, partial
  writes/lost acknowledgements and read-only recovery. Unexpected extra files, symlinks, executable
  files, submodules, changed PR metadata/head, auto-merge and ready/merged PRs fail closed.
- A separately approved compensation closes only the verified unchanged unmerged draft PR. It never
  deletes a branch or reverts a merged change. Partial branch/commit writes without a PR stay quarantined
  for operator investigation; no retry or cleanup is automated.
- Stage 4 local security baseline: **91/91**; Stage 5 local lifecycle baseline: **42/42**. Independent
  external acceptance remains incomplete, including GitHub receiver qualification.
- Ruff lint/format, whitespace, frozen lock consistency and offline source/wheel builds pass.
- Tests use synthetic manifests and a stateful fake GitHub transport. No real target repository has
  been enrolled and no live PR, merge or closure was performed. The adapter currently supports only
  github.com and a pinned non-secret JSON object interval; see the runbook for assembly and limits.
