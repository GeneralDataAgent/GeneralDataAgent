# Stage 5 business-loop development evidence

Date: 2026-10-18 (Asia/Shanghai). Branch: `feat/stage5-business-loop`.

## Delivered repository scope

| Roadmap delivery | Implementation | Boundary |
|---|---|---|
| Dashboard, ticket, scheduling PR, registration, IM and report actions | Six discriminated strict payloads, authenticated draft/status/execute/cancel API, persisted task/thread and exact payload | Transactional local draft backend only; no external publication, message transmission or deployment |
| Idempotency, approval, expiry, cancellation | Durable actor-scoped key/content conflict detection, exact one-time approval, separate requester/reviewer, replay receipt | Interrupted consumed/claimed actions quarantine for reconciliation |
| Compensation/rollback | Separately approved compensation referencing original content and current revision | Retracts the matching local resource only; cannot overwrite a later revision |
| Execution guards | Current session, scope, DLP, policy, destination revision, per-kind owner/switch/rate and global switch | Controls are trusted configuration; no approval/control mutation HTTP endpoint |
| Delivery evidence and audit | Intent/payload/receipt hashes, transactional resource/receipt/completion and chained local events | Independent external audit/retention and receiver reconciliation still required |
| Periodic reports | Durable finite schedule, bounded tick, stable occurrence IDs, independent approval per occurrence, skip expired periods | Fixed evidence snapshot; fresh source content requires a new reviewed schedule; no unattended sending |
| Cost/latency, routing/cache | Opt-in async admitted-model executor, bounded tokens/cost/deadline, live authorization checks, deterministic configured-cost route, encrypted 15-minute-max scoped cache | Operator price/latency estimates; not connected to the existing Supervisor or a live model by default |
| User feedback/evaluation governance | Completed metric deliveries register scoped hashes; authenticated feedback, DLP, two independent reviews, immutable datasets and task/evidence holdout separation | Local trusted reviewer ports; no automatic training or production review service |
| Agent/Skill/connector admission | Pinned immutable version/evidence/owner/scope/expiry/rollback, independent quality/security reviewers, activation and permanent revocation | Metadata admission only; no executable loading or automatic tool-permission expansion |
| Independent per-action acceptance | Signed, revision/configuration/environment-bound external receiver measurements, safety/compensation/audit/business targets, separate result for every action kind | Missing evidence stays `not_run`/incomplete; no fixture can establish a production claim |

## Validation

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

The aggregate result remains **incomplete** even if all per-action bundles pass: independently
measured production cost/latency improvements and Stage 4 production admission are still mandatory
and are not supplied or waived by this local runner. `--require-production` therefore exits 1.
This phase provides local development workflows and qualification tooling, not production release.

## Known limitations

Local state requires protected filesystem access and backups; chain verification cannot detect a
complete rollback without an independently stored checkpoint. Draft evidence hashes identify the
proposed references; a local draft receipt does not verify that an external system fetched or
accepted those sources. Hash/existence/authorization validation against real artifacts belongs in
the qualified receiver adapter. Report refresh, external credentials, receiver-specific payload
mapping, production retention/legal holds and provider tokenizer/billing checks remain external
integration work. Prices and latency estimates are trusted inputs, not measurements.
