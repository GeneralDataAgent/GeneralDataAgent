# Stage 4 controlled pilot runbook

Status: repository controls implemented incrementally; production admission is not approved.

## Deployment prerequisites and trust boundaries

The service owner provisions HTTPS ingress, a real OIDC authorization-code client with browser-bound
state/PKCE, operator-pinned issuer keys and a server-side user/project/domain grant resolver.
Only the trusted login controller invokes `OIDCSessionAdapter.exchange`. Never accept tenant,
project, role, source credentials or signing keys directly from JWT custom claims/API bodies.

Use `SQLiteSessionBroker` only on a single host with a shared, protected state volume. Agents
must not mount the session, approval, task ownership, artifact index or login challenge databases.
For multiple hosts replace these local ports with an external identity/state service and repeat
concurrency, revocation and recovery qualification. Backups contain identity metadata and require
access controls and independent retention. Remove revoked OIDC keys from the trusted key map.

Use a secured `create_app` factory with `production_mode=True`; the module-level `app` is a local
development entry point. Configure a fail-closed service switch, scoped review store, bounded SLO
collector and matching pending-action/session brokers. Inject `GovernedArtifactStore` into each
runtime using the same tenant/project/domain scope as its actor. Never reuse an actor-bound store
for another tenant. Use `VerifiedAuditSink` with an independently administered append-only service,
append-only application credentials, operator-pinned public keys and a runtime-only HMAC secret.
No private signing keys, bearer tokens or pseudonym secrets belong in Git or ordinary logs.

The existing SQLite/DuckDB connectors do not establish native RLS/CLS. Before production, provision
short-lived workload credentials and native database row/column permissions, and produce independent
cross-user/cross-tenant source-query evidence. An authenticated API session alone is insufficient.
Likewise an OCI contract or a local fixed-template sandbox is not a real sandbox penetration review.

## Admission and staged rollout

1. Run the normal formatting, lint, test/coverage and locked-build checks.
2. Run `uv run --frozen python evals/run_stage4_acceptance.py`. Local security regressions must pass.
3. Security and operations reviewers independently measure and sign a release bundle for the exact
   committed revision and intended staging/production environment. The private keys stay outside
   the application and repository. Trust registry entries contain signer ID, role and base64 raw
   Ed25519 public key; remove a compromised signer before any new gate evaluation.
4. Verify with `uv run --frozen python evals/run_stage4_acceptance.py --require-production
   --environment staging --release-bundle /protected/release.json
   --trusted-signers /protected/trusted-signers.json` (one command). A nonzero exit blocks rollout.
5. Provision an explicit small user/domain allowlist at the trusted grant resolver. Keep the
   previously known-good image and policy/model/prompt/dependency versions available for rollback.
6. Check health, identity rejection, revocation, scoped review access, a harmless approved fixture,
   DLP denial, audit acknowledgement and independent source permissions before enabling traffic.
7. Start one pilot cohort; review failures, security denials and costs daily. Expand only after
   at least 14 clean days, with no blocker and independently measured release/SLO thresholds met.

The 91-case security baseline is not the required 90-task real-work success benchmark. A signed
bundle must contain that independent task measurement plus real exercises and observations.
The bounded in-process SLO snapshot is diagnostic, not a substitute for monthly monitoring.

## Incident handling and rollback

| Trigger | Immediate action | Evidence and recovery |
|---|---|---|
| Cross-scope access, leaked credential or unexpected write | Close service switch; revoke affected sessions and source credentials | Preserve independent audit IDs; security owner classifies impact; rotate secrets and test the original exploit before reopening |
| Invalid/missing audit receipt | Keep tools blocked; page audit/service owner | Verify remote append health and pinned keys; never switch production to unaudited execution |
| Identity provider outage | Reject new login; existing sessions expire normally | Restore provider and verify nonce/signature/issuer checks; never fall back to header identity |
| Provider/model outage or cost spike | Stop new affected tasks; keep budgets enforced | Use only a previously qualified fallback; record version and rerun failed tasks under original budgets |
| Elevated latency or 5xx | Close or reduce pilot cohort | Compare aggregate SLO samples and independent telemetry; roll back to known-good image/config |
| Interrupted approved action | Stop automatic retries | Reconcile `executing`/`failed` pending state against destination side effects and independent audit; the one-use approval cannot be replayed |
| Artifact reaches retention deadline | Deny reads/exports immediately; run expiry sweep | Verify deleted bytes and tombstone; legal holds block deletion only; apply independent backup/trace retention |

Critical alerts must reach the on-call owner within 300 seconds, measured by the alerting system.
The repository does not send unconfigured external notifications. Health stays available when the
switch stops versioned traffic. Drain/cancel in-flight tasks and reconcile approvals before rollback;
the switch blocks new requests and does not claim to cancel work already executing.

## Disaster recovery drill

Freeze ingress and capture a consistent protected-state backup plus artifact content/index and
independent audit sequence. Restore into an isolated environment with no production write routes.
Verify immutable task ownership, two-party review totals, expired/revoked sessions, consumed grants,
pending action non-replay, artifact hashes, legal holds and deleted-content tombstones. Revoke all
restored sessions before reopening traffic; a backup may predate a revocation. Reconcile approval
and audit state with the authoritative destination, then issue new sessions through SSO. Record
RTO/RPO and every failed invariant in a signed drill report. A local unit test is not a completed
recovery drill or externally measured uptime result.
