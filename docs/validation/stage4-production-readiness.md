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

## Increment 16: durable critical alerts

`CriticalAlertRouter` persists notification intent, accepts only fixed critical event categories,
and passes the event UUID as a destination idempotency key. Delivery receipts bind that event,
measure actual notification latency and distinguish pending/late (>300-second) notifications.
Restart/retry preserves intent; acknowledgement loss requires destination-side idempotency.
Repeated gateway audit outages produce one notification per five-minute category window while
keeping tools blocked. `/v1/operations/alerts` exposes aggregate status under operations permission.
No real recipient/endpoint is configured or contacted by tests; external paging and independent
monitoring of pending notifications remain deployment obligations.

## Increment 17: durable task budgets

`TaskBudgetLedger` fixes task actor scope, deadline and call/token/scan/cost ceilings at registration.
Atomic reservations across workers survive restart; repeated registration cannot reset usage or
extend deadlines. Negative/type-confused usage and cross-actor reservations are rejected. Gateway
checks reservations before handler execution (and before consuming an approval). Production analysis
requires this ledger and currently disables unmetered LLM narration. Tool calls and task deadlines
are enforced in the integrated runtime; token/scan/cost accounting requires trusted source/provider
metering adapters to reserve those amounts and must be independently qualified. Tests race 16
reservations against a three-call ceiling and verify that exhausted budgets stop tool execution.

## Increment 18: encrypted durable checkpoints and analysis resume

`ProtectedSqliteSaver` uses the official SQLite checkpoint adapter with AES-256-GCM serialization,
no pickle fallback, encrypted actor scope and minimal plaintext routing metadata. A protected
runtime key is supplied outside Git; agent/API code has no key-management endpoint. Thread
ownership is immutable, expired reads are denied and operator sweeps purge checkpoint/write
rows, preserving scope tombstones; trusted legal holds defer deletion. Keys/backups still need
operator lifecycle/retention. Production analysis now requires this checkpointer.
`POST /v1/analysis/tasks/{thread_id}/resume` authenticates current permission and original scope,
checks the existing budget, and resumes the saved graph with the same task/thread instead of
creating a replacement. Completed retrieval does not requery. Tests interrupt and reopen 100
workflows across four node boundaries; all resume without rerunning completed analysis. On-disk
DB/WAL scans confirm fixture results, questions and tenant IDs are not plaintext. This local
checkpoint qualification is not a real infrastructure disaster-recovery attestation.

## Increment 19: production trace and access-log boundary

Production graph execution/resume explicitly disables environment-enabled external LangSmith
tracing so raw checkpoint state and query results cannot leak through an ambient trace exporter.
Independent minimal signed audit and aggregate SLOs remain available. A regression enables the
tracing environment variable and checks that the executing source still observes tracing disabled.
The built-in API launcher disables access logging to avoid logging OAuth callback codes/query
strings. A separately configured proxy/launcher must enforce the same boundary. Approved redacted
trace export is not enabled by this pilot.

## Increment 20: deployable independent audit collector and HTTPS transports

`services.audit_collector` provides a separately deployed, append-token-only HTTP service.
It rejects raw/non-minimized identities or payloads, bounds streamed requests, atomically deduplicates
event IDs, persists an append hash chain and signs durable receipts with a service-owned Ed25519 key.
There are no read/delete/admin HTTP routes. Operator chain verification can check an independently
anchored sequence/hash; a self-contained chain does not prove absence of tail truncation without
that anchor. `AuditHTTPTransport` and `AlertHTTPTransport` pin fixed HTTPS endpoints, refuse redirects,
bound responses and sanitize failures. Integrated collector/receipt tests cover retries, concurrent
deduplication, tampering, credential denial, payload limits and absent destructive endpoints.
Deployment still requires separate OS/service credentials, protected volumes and externally anchored
backups/retention; in-process tests do not establish that administrative separation.
SQL parser fallback warnings are also redacted so untrusted literals do not enter ordinary logs.

## Increment 21: live release admission on service startup and requests

`create_released_app` is the deployment factory. It requires a shared single-host session broker,
valid independent release evidence for the exact built revision/environment and a live service
switch; development-mode override is rejected. `FileReleaseAdmission` reloads operator-owned
bundle/trust files without a permissive cache. Expired/revoked/changed/missing proof blocks new API
work and login immediately, while health stays available. The same strict trust loader is used by
the CLI. Tests revoke trust on a running API and verify that new requests stop without restart.
Deployment must bind the expected revision to immutable build provenance; unsigned source changes
or handwritten evidence are never a substitute for the signed release pipeline.

## Local qualification: 2026-10-18

At implementation commit `66690ab`:

- Locked dependency consistency, Ruff lint/format and diff whitespace checks passed.
- Full suite: 262 tests passed; line coverage 87.93% (required >=85%).
- Frozen security matrix: 91/91 passed, dataset SHA-256
  `ce97f755e446ddac692bf0e5b1ba7ef45131f6df6e2d3c039c32acac137e02c0`.
- Encrypted checkpoint interruption/restart exercise: 100/100 resumed without replaying a completed
  analysis node. This is included in the full suite, not 100 additional top-level test functions.
- Source and wheel distributions built successfully with the locked environment.
- Aggregate Stage 4 development gate passed. Production gate remains `incomplete` because independent
  real-environment release evidence is absent; strict production mode returns a failing exit code.

Remaining requirements are actual provider/native-source enrollment and independent deployment,
security/DR/provider drills, real-work success measurements and observed SLO/pilot evidence.
No real SSO user, production data source, paging recipient or deployed environment was fabricated.

## Follow-up 1: semantic JSON/Unicode DLP checks

A post-integration review reproduced egress bypasses through escaped JSON keys/values and fullwidth
credential field names. Text DLP now scans both original and decoded JSON representations with NFKC
normalization and format-character removal. It bounds JSON nesting before parsing and rejects
non-finite structured values. Original artifact bytes are preserved; failed scans never write content.
Tests cover escaped credentials/email/phone, hidden separators, nested data, safe encoded reports and
resource-exhaustion input. This closes these specific encoding paths, not arbitrary PII detection.

## Follow-up 2: concurrent analysis resume and retention fencing

Analysis run/resume now claims a shared SQLite execution lease before reading or advancing a graph.
A second authorized request receives HTTP 409 rather than replaying an active analysis. Heartbeats
renew the lease; stale leases can be reclaimed after process loss, and the old owner cannot return
success or append checkpoint writes after losing ownership. Retention sweeps skip active leases;
expired/deleted threads reject subsequent checkpoint and pending-write persistence. Tests exercise
independent saver connections, concurrent API resume, stale-owner fencing, lease recovery and
expiry/cleanup interaction, alongside the existing 100 restart cases. These are read-only analysis
leases; approvals retain their separate durable one-use side-effect and reconciliation rules.

## Follow-up 3: strict durable identity records

Durable sessions now validate a strict schema for bounded nonempty opaque identity/domain fields,
explicit permission enums, collection types and timezone-aware expiry. Malformed records (including
naive expiry and string-valued permissions/domains) produce a fixed 401 instead of a server error
or implicit coercion. Issuance applies the same bounds; bearer input is bounded before hashing.
Legacy records without permissions retain no action grants. Regression tests corrupt individual
stored fields and verify that neither the original record nor validation details reach responses.

## Follow-up 4: absolute SSO/session deadlines

Session issuance accepts a strict aware absolute `not_after` ceiling and never rounds a remaining
fraction of a second upward. OIDC rechecks expiry after the trusted grant resolver returns and
passes the issuer's absolute expiry to the broker, so slow resolution/nonce persistence cannot
extend an expired identity token into a new session. Tests advance a simulated resolver past token
expiry and verify no session is created, plus exact fractional deadline and maximum-TTL boundaries.

## Follow-up 5: service-owned retries and retention sweeps

`ServiceMaintenance` is now wired to the API lifespan when explicitly configured. It retries durable
pending critical alerts with the original event IDs and periodically sweeps the supplied artifact
and checkpoint stores, without depending on new user traffic. Failures are isolated per job and
reported as aggregate counters through the protected operations endpoint; no exception payloads or
sensitive records are exported. Shutdown stops new retry batches and joins the worker. Tests cover
outage/recovery without new requests, original alert idempotency keys, legal holds, isolated cleanup,
worker lifecycle and operations authorization. Only configured stores/transports are touched.

## Follow-up 6: concrete PostgreSQL native permission adapter

`PostgresNativeDriver` opens a fresh read-only transaction with the short-lived workload role.
It checks native role expiry/privileges, immutable operator-owned scope registration, FORCE RLS,
the independently provisioned policy digest, and exact effective column SELECT privileges before
executing guarded SQL. Row visibility comes from PostgreSQL policies. Parameter conversion retains
lexical binding order through CTEs and preserves literal percent signs. TLS verification defaults
to `verify-full`; plaintext requires an explicit loopback development setting.

The digest-pinned PostgreSQL 17 disposable runner exercises 19 real-database checks, including
cross-tenant rows, private columns, role/actor substitution, policy and ACL drift, BYPASSRLS,
mutable scope metadata, row bounds, and parameter ordering. CI runs this integration inside the full
coverage suite on Python 3.11/3.12/3.13. All fixtures are synthetic and local/CI only; production
credential issuance, provider enrollment, TLS and independent source qualification remain open.

Native semantics follow the PostgreSQL 17 documentation for
[row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) and
[privilege inspection](https://www.postgresql.org/docs/17/functions-info.html), with server-side
binding through [Psycopg parameters](https://www.psycopg.org/psycopg3/docs/basic/params.html).

## Follow-up 7: signed audit recovery integrity

Collector chain verification now checks each stored receipt's Ed25519 signature and its binding
to the sequence, event ID and content hash, including canonical event identity. Recomputing an
altered event's unsigned hash chain no longer passes verification. Idempotent append also rejects
corrupt stored receipts. Historical signer public keys must be explicitly retained through
`trusted_receipt_keys` when rotating the collector signing key; unknown historical signers fail
closed. Tests cover signature, signer, timestamp, sequence and hash corruption, full event rehashing,
and rotation with/without explicit historical trust. Operators still need independently retained
sequence/hash anchors to detect tail truncation.
