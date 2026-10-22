# Controlled pilot runbook

This runbook describes configuration, rollout, and recovery. See the [external validation record](../../../docs/validation/stage4-production-readiness.md) for admission status.

## Deployment prerequisites and trust boundaries

The service owner provisions HTTPS ingress, an OIDC provider registration, operator-pinned issuer keys and a server-side user/project/domain
grant resolver. The repository supplies browser state/PKCE and a fixed HTTPS code-exchange client.
Configure `OIDCLoginFlow` on the API factory with the fixed registered HTTPS redirect URI.
Disable query strings in proxy/application access logs, especially `/auth/callback`; authorization
codes and login state must not enter logs. Only the login controller invokes `OIDCSessionAdapter.exchange`. Never accept tenant,
project, role, source credentials or signing keys directly from JWT custom claims/API bodies.

Use `SQLiteSessionBroker` only on a single host with a shared, protected state volume. Agents
must not mount the session, approval, task ownership, artifact index or login challenge databases.
For multiple hosts replace these local ports with an external identity/state service and repeat
concurrency, revocation and recovery qualification. Backups contain identity metadata and require
access controls and independent retention. Remove revoked OIDC keys from the trusted key map.

Deploy through `api.production.create_released_app` with `FileReleaseAdmission` bound to the
immutable build revision and protected signed bundle/trust files. It validates admission at startup
and on new work/login. `create_app(production_mode=True)` is the composition/testing port; the
module-level `app` is a local development entry point. Configure a fail-closed service switch, scoped review store, bounded SLO
collector and matching pending-action/session brokers. Inject `GovernedArtifactStore` into each
runtime using the same tenant/project/domain scope as its actor. Never reuse an actor-bound store
for another tenant. Use `VerifiedAuditSink` + `AuditHTTPTransport` with the separately deployed
`services.audit_collector` service,
append-only application credentials, operator-pinned public keys and a runtime-only HMAC secret.
Configure `CriticalAlertRouter` + `AlertHTTPTransport` with an operator-approved paging service that
honors event-ID idempotency. Do not expose collector storage, signing key or administration to agents.
Production analysis also requires `TaskBudgetLedger` and `ProtectedSqliteSaver`; supply its 32-byte
AES key from protected runtime configuration, retain the key for authorized recovery, and invoke
checkpoint/artifact expiry sweeps under operator control. Ambient raw LangSmith export is disabled.
No private signing keys, bearer tokens or pseudonym secrets belong in Git or ordinary logs.

The existing SQLite/DuckDB connectors do not establish native RLS/CLS. Before production, provision
short-lived workload credentials and native database row/column permissions, and produce independent
cross-user/cross-tenant source-query evidence. An authenticated API session alone is insufficient.
Likewise an OCI contract or a local fixed-template sandbox is not a real sandbox penetration review.

### PostgreSQL native source setup

The concrete `connectors.postgres_native.PostgresNativeDriver` can supply the native probe to
`ScopedQueryExecutor`. Configure a fixed endpoint/database, relation and column allowlists, trusted
CA and operator-approved policy digests. Capture `postgres_policy_digest` using a trusted provisioning
connection only, and review that policy's intended scope; never accept a digest sampled from an
untrusted runtime session as its own approval.

Provision expiring non-owner LOGIN roles without superuser, BYPASSRLS, CREATEROLE, CREATEDB or
replication privileges. Grant SELECT on only the approved columns, enable and FORCE RLS, and put
the role's user/tenant/project/domain mapping in an operator-owned `governance.role_scopes` table
with `role_name`, `user_id`, `tenant_id`, `project_id`, `data_domains` columns. The workload role
must be able to read its mapping and unable to mutate it. The trusted lease provider supplies
that role's protected credential and a deadline at least as short as the maximum 15-minute lease;
the database role expiry must not outlive that lease. The driver does not mint roles or run admin
SQL. Configure `ServiceMaintenance` for the intended artifact/checkpoint stores and alert router;
its protected operations snapshot reports retry/sweep failures without exposing record contents.

For the synthetic local qualification, first obtain the pinned official image below, then run:

```sh
docker pull postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
uv run --frozen python evals/run_stage4_postgres.py
uv run --frozen python evals/run_stage4_postgres.py --pytest
```

The runner creates and removes its own constrained temporary container, publishes only a random
loopback port, and generates disposable credentials in a protected temporary file. It never connects
to an existing production database. These results qualify local adapter behavior; real endpoint,
credential lifecycle, native policies and TLS still need independent deployment evidence.

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
