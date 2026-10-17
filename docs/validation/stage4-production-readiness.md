# Stage 4 production readiness

## Increment 1: scoped short-lived identity

`create_app(session_broker=..., production_mode=True)` requires a trusted session broker.
An external SSO adapter authenticates the user and resolves authorized project/domain grants
before calling `SessionBroker.issue`; there is deliberately no public token-issuance endpoint.
Sessions expire within 15 minutes, support revocation, and store only token digests.
Every `/v1/` request must match the session's user, tenant, project and authorized domains.
Health remains public. Review endpoints fail closed until their storage has tenant ownership.
The default app remains a local development surface; production must use the secured factory.

Validation: expiry boundary, revocation, forged user/tenant/project/domain, missing credentials,
closed review access and missing production broker. Local sessions invalidate on restart and
are not suitable for multi-worker deployment; external SSO, shared session storage and native
source RLS/CLS remain required before production acceptance.

## Remaining production acceptance work

Repository increments below provide local controls and executable release checks. Production
acceptance still requires real OIDC browser/code integration, native source RLS/CLS/workload
credentials, independently administered audit/storage, actual security/disaster/provider drills,
90+ real-work tasks and observed SLO/pilot evidence. These cannot be replaced by local fixtures.

Stage acceptance remains incomplete until independent deployment evidence exists.

## Increment 2: service stop and release measurements

The factory accepts a trusted `service_enabled` callback. False, non-boolean values or
control-plane errors stop all `/v1/` requests with 503, while health remains available.
The switch blocks new requests; draining/cancelling in-flight work is still an operator duty.
`security.release.release_blockers` checks revision, 24-hour evidence freshness, at least
90 tasks and 88% success, zero security blockers, at least 100 recovery attempts and 99%
success, plus the mandatory exercise set. This is a measurement evaluator, not a deployment
authorizer: evidence signatures, SLO observation and independent approvals remain external.

## Increment 3: minimal audit payloads

`MinimalAuditSink` is an opt-in local audit adapter with a fixed payload allowlist.
It preserves only nonnegative integer counters and boolean flags, dropping unknown fields,
free text, nested objects and type-confused values before writing. It does not mutate the
source event. Envelope identities must be opaque trusted service identifiers. The existing
development sink remains available; deployments must explicitly inject this restricted sink.
This is payload minimization, not full DLP: trace providers, artifact contents, retention,
independent audit authorization and envelope validation still require separate controls.

## Increment 4: scoped durable human reviews

`ScopedHumanReviewStore` persists immutable task tenant/project/domain ownership in SQLite.
The analysis runtime registers task scope server-side; no API accepts ownership registration.
Secured review endpoints authorize that ownership and bind reviewer IDs to authenticated users.
Duplicate reviews are rejected atomically across independent connections; restarting preserves
both ownership and the dual-review gate. Unknown and foreign task IDs return the same denial.
Legacy unscoped JSONL ledgers remain development-only and are never implicitly imported.
Validation includes cross-scope reads/writes, forged reviewers, restart and 16 racing submissions.

## Increment 5: shared local sessions

`SQLiteSessionBroker` persists only bearer digests and identity grants. Independent API
processes sharing the protected database observe issuance, expiry and revocation; restart
no longer invalidates these sessions. Validation covers cross-instance revocation, expiry
at the exact boundary, restart and absence of raw bearer tokens in the database.
This is a single-host shared registry, not a distributed identity service.

## Increment 6: original-thread approval resume

`PendingActionCoordinator` persists original request parameters, approval ID and thread ID.
`POST /v1/actions/{thread_id}/resume` accepts no replacement action parameters and requires
current authenticated identity. The coordinator revalidates user/tenant/project/domain,
current risk policy and an unconsumed, unexpired grant before claiming the action. ToolGateway
then repeats policy and atomic grant consumption. A grant not yet issued leaves the action
pending. Concurrent resume calls execute at most once; a crash or execution failure requires
operator reconciliation and is never automatically replayed. R2 and R3 tests use inert fixture
handlers, not production writes. Pause/approve remain trusted runtime/approval-service ports.

## Increment 7: governed text artifact lifecycle

`GovernedArtifactStore` implements the existing artifact port with an actor-bound scope,
DLP-before-write and immutable metadata stored outside the content tree. Reads and exports
validate tenant/project/domain, reference integrity and expiry. Text scanning rejects private
keys, credential assignments, bearer tokens and common email/Chinese mobile patterns; unknown
encoding and oversized input fail closed. Export additionally rejects confidential/restricted
classification. This is a conservative UTF-8 pilot and does not claim universal PII detection.
Expired content is denied immediately; an operator sweep deletes bytes with durable tombstones.
Legal holds prevent deletion but do not grant expired content read access. Tests exercise
cross-scope reads, forged classification, hash tampering, expiry, restart, legal holds, and
idempotent deletion. Backup retention and external trace deletion remain separate obligations.

## Increment 8: frozen security regression and SQL boundary fixes

`evals/datasets/stage4-security.jsonl` freezes 91 local cases spanning SQL mutation,
CTE-scope bypass, file/network table functions, unreviewed scalar functions, prompt-like
untrusted text, DLP egress, forbidden tools, role escalation and mandatory R2/R3 approval.
Positive read-only/aggregate and safe-text cases guard against blanket-denial regressions.
The evaluator emits case IDs, outcomes and dataset digest, without raw attack content.
SQL inspection now rejects SELECT INTO and external/unreviewed functions, and resolves CTE
ownership lexically rather than whitelisting CTE names globally. This local security suite is
not the separate 90-task production success benchmark and not a penetration-test attestation.

## Increment 9: independently signed audit acknowledgements

`VerifiedAuditSink` sends minimal events through an injected append-only transport and checks
Ed25519 receipts against pinned public keys, event IDs/content hashes and five-minute freshness.
Envelope identities are HMAC-pseudonymized with a deployment secret; SQL/prompts/free text are
never forwarded. Service errors return a fixed denial without echoing remote credentials.
`ToolGateway(production_mode=True)` requires this sink and refuses to execute without a valid
acknowledgement. Tests cover forged/replayed/mismatched/expired/future receipts and service
outage before tool execution. Operators must provision a separate service identity, remote
append-only storage and signing key; local tests do not attest to that infrastructure.

## Increment 10: SLO measurements and operational release thresholds

`ServiceSLO` keeps a bounded aggregate latency/availability sample window with no request
payloads or user labels. API middleware records success/error availability and elapsed time;
the protected operations endpoint exposes aggregate counts and nearest-rank P95. The rolling
sample is diagnostic and is not a monthly uptime measurement. Release evidence now separately
requires a 30-day observation, >=99.5% time availability, >=100 event samples, P95 <=3 seconds,
critical alert delivery <=300 seconds and >=14 clean pilot days. Missing/non-finite values fail
the gate. Real observation and alert transport remain deployment obligations.

## Increment 11: trusted OIDC identity adapter

`OIDCSessionAdapter` uses pinned RSA keys and PyJWT RS256 verification with exact issuer and
single audience. It requires subject/expiry/issue-time/nonce claims, rejects untrusted key URLs,
checks authorized party and token age, and consumes a persisted one-use login challenge before
issuing the existing short-lived session. Tenant/project/domain grants come only from a trusted
server-side resolver, never token custom claims. Tests include forged keys, symmetric algorithm
confusion, key URLs, wrong issuer/audience/nonce, expired/future tokens and 16 concurrent replays.
The trusted web controller still must implement browser-bound OAuth state, PKCE/code exchange,
TLS and real provider enrollment; this adapter is deliberately not a public login endpoint.
Design references: [OIDC Core ID Token Validation](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation)
and [PyJWT verification API](https://pyjwt.readthedocs.io/en/stable/api.html#jwt.decode).

## Increment 12: executable signed release gate and operations guide

`ReleaseBundle` binds strict measurements, environment and expiry to the committed revision.
Verification requires distinct security/operations signers and distinct Ed25519 public keys;
missing trust, tampering, stale evidence, dirty source and failed local regression block production.
`evals/run_stage4_acceptance.py` runs the 91-case local baseline and optionally the signed gate;
`--require-production` returns nonzero while any admission blocker remains. CI now runs the local
baseline on main, PRs and Stage 4 feature pushes. Private signing keys and evidence stay outside
Git. [The operations runbook](../operations/stage4-runbook.md) covers cohort rollout, rollback,
identity/audit/provider outages, approval reconciliation, retention and disaster recovery.

## Increment 13: browser OIDC authorization-code login

`/auth/login` creates a Secure/HttpOnly/SameSite=Lax browser binding and durable state,
nonce and PKCE S256 challenge. `/auth/callback` consumes matching unexpired browser state
once before exchanging the code and verifying the ID token. It returns a short-lived bearer
with no-store/no-referrer headers, clears the login cookie and rejects foreign-browser and
callback replay. The fixed HTTPS token client refuses redirects and bounds responses/timeouts;
errors never echo codes or credentials. Provider endpoints, client registration and grant
resolver are operator configuration. Configure reverse proxy and app access logging to omit
callback query strings; no real provider login is claimed by the local end-to-end fixture.

## Increment 14: production runtime and native source boundary

`ScopedQueryExecutor` propagates the current actor to a trusted short-lived credential issuer,
limits leases to 15 minutes, checks source relation grants, and requires the opened native session
to confirm matching source/role/user/tenant/project/domains with RLS, CLS and read-only enabled,
without superuser or bypass-RLS. Expiry is checked before and after query execution; source errors
are sanitized. Native driver implementations remain trusted and must be qualified on the real DB.
`MetricAnalysisRuntime(production_mode=True)` requires this scoped executor, governed artifacts and
verified audit; the production API refuses a development runtime. Actor-bound artifact scope is
checked before analysis. Production science similarly requires governed artifacts, independent
audit and OCI sandbox. Gateway audit now includes the request fingerprint for action binding.
An integrated local fixture exercises API -> actor -> credential lease -> live-scope check -> query
-> governed artifacts -> verified audit. It is not an external database/RLS attestation.

## Increment 15: user permission scopes

Sessions now carry explicit server-issued action permissions. Authentication alone grants no
analysis/science execution, action resume, review submission/read or operations access. The API
checks the required permission before dispatch; pending-action resume repeats its permission
check. `IdentityGrant` lets the trusted OIDC grant resolver supply permissions; an ActorContext-only
grant issues an unprivileged session. JWT custom permissions and caller headers are ignored.
Permissions persist in shared session state; old records without them default to no action grants.
Tests cover cross-role attempts, fake permission headers and shared-store reload. Login transaction
cleanup now removes expired/consumed state and avoids issuing a nonce when capacity is exhausted.
