# Stage 5 local business-loop runbook

## Configure the authenticated draft pilot

Create a `BusinessActionRuntime` with a shared session broker, a separately protected approval
store/database and explicit `ActionControl` entries. Every control binds kind, destination,
owner, tenant, project, domains, independent enabled switch and per-minute quota. The empty
configuration denies every action. `service_enabled` provides an additional global kill switch.
The runtime and scheduler are optional `create_app` dependencies. They are absent by default.
Production mode rejects this local draft pilot pending independent qualification.

Grant `business:propose` for creating/cancelling drafts, `business:read` for reads and
`actions:resume` for executing approved drafts. The existing bearer session and scope headers
are mandatory. No caller can supply an actor identity in a draft body. The server records it.
No public route grants approvals; a trusted approval operator must review exact content and use
the approval service, with a reviewer distinct from the requester.

Endpoints:

- `POST /v1/business/drafts`: strict `ActionIntent` with one of six payload kinds.
- `GET /v1/business/drafts/{action_id}`: owner-scoped persisted content/status/receipt.
- `POST /v1/business/drafts/{action_id}/execute`: original persisted content only.
- `POST /v1/business/drafts/{action_id}/cancel`: pending actions, including approved/unconsumed.
- `POST /v1/business/schedules`: bounded `ReportSchedule`; optional scheduler dependency.
- `POST /v1/business/schedules/{schedule_id}/tick`: generate at most ten due report drafts.
- `POST /v1/business/schedules/{schedule_id}/cancel`: stop future occurrences.

## Review and recover

Review destination, task/thread, payload, expected revision, expiry and rollback before approval.
Compensation is a new intent with operation `compensate`, original action ID, identical payload
and destination, and the currently expected revision. It requires a new approval. It retracts only
the matching local draft; it cannot unsend an external notification or change a deployed model.

A completed retry returns its existing receipt. An `executing` or `uncertain` action requires
operator reconciliation: inspect local resource state, approval consumption and the chained audit
history before issuing a replacement. Do not reset the row to pending or reuse consumed approval.
If the revision changed, inspect the newer content and submit a newly reviewed action.

The scheduler stores no bearer token and sends nothing automatically. Call ticks with a fresh
scoped session from an operator-controlled job. Occurrence IDs and idempotency keys are stable
across retries and restart. Each occurrence gets a separate approval and finite expiry. Expired
slots are recorded as skipped; no catch-up publication occurs. A cancelled schedule does not
cancel drafts already generated: cancel those drafts explicitly. Report hashes point to a fixed
approved snapshot; refreshing report content requires a new schedule/version and evidence review.

## Boundaries

`local_draft` receipts explicitly report `external_delivery=false`. They establish local outbox
persistence and exact content, never actual IM delivery, PR creation, Dashboard publication or
registry acceptance. Protect the databases and their backups as runtime state; bodies are DLP
checked but still contain internal business information. Local chain verification requires an
independent checkpoint to detect whole-history rollback. Retention/legal holds for external systems,
receiver-side idempotency, authorization, real compensation and business-value measurement remain
per-adapter qualification gates. Do not enable external delivery based on local tests alone.

## Feedback and capability governance

An optional `FeedbackRegistry` can be supplied to `create_app`. Completed metric deliveries register
the exact result hash against the authenticated task scope. `POST /v1/business/feedback` accepts only
feedback for that registered task and result. DLP runs before persistence. The endpoint returns
`awaiting_independent_review`, never an automatic promotion to training data.

Trusted operators call `review` with two distinct reviewer identities other than the feedback author,
then `freeze` an immutable, hashed evaluation/training dataset. Any rejecting review blocks promotion.
Both task IDs and evidence hashes are checked across splits; rewording the same result cannot bypass
holdout separation. Dataset scope cannot mix tenants, projects, authors or authorized domains.

`CapabilityRegistry` tracks Agent, Skill, connector and model versions with pinned content and
regression/security evidence hashes, owner, scope, rollback reference and expiry. Activation requires
distinct quality and security reviewers other than the owner. Revocation is permanent for that
version; ship a new reviewed version rather than resetting it. `require` rechecks the version,
content hash, scope, active state and expiry immediately before use. These trusted local review
ports need an external authenticated review service and independent evidence verification before
production use. Admission never grants new tool permissions or loads executable code.

## Model routing and cache

`GovernedModelExecutor` is an optional async adapter port. Supply only pinned, admitted model routes
and bounded async provider adapters. Every route has an independent enabled switch, permitted
purposes/classifications, token prices and estimated latency. Selection picks the lowest configured
cost within the request's cost and latency bounds; current authorization and capability admission
are checked before selection, before invocation and after completion/cache access.

Register the task in the existing `TaskBudgetLedger` first. The executor reserves worst-case input
plus output tokens and configured cost before calling a provider; failed calls keep the reservation.
It enforces an async deadline and rejects reported token overruns. No automatic fallback expands
permissions, data classification or budget. Actual provider billing and tokenizer accuracy require
adapter qualification; configured estimates are not a measured production cost improvement.

`ScopedResponseCache` requires an external encryption key and at most a 900-second TTL. Its key binds
actor/tenant/project/domains, query, exact source snapshot, model/content hash, prompt, policy and
authorization versions. Encrypted records bind their key and timestamps, preventing row substitution
or expiry extension. Cache hits still require live authorization, admission and task deadlines.
Use fresh authorization/source versions from trusted systems, never user-supplied versions. Run
`purge` during maintenance; expired records are also removed on reads/writes. Cache ciphertext and
keys are runtime state and must not be committed. The existing Supervisor is unchanged: this port
must be explicitly wired by a qualified deployment. Do not enable the local pilot in production.

## Routed narration in the analysis workflow (2026-10-19)

`MetricAnalysisRuntime(routed_narrator=..., budgets=...)` now invokes the governed executor from
its normal analyze node; the existing Supervisor's certified-query tool uses that same runtime.
The routed narrator must share the exact task budget ledger with SQL execution. Legacy and routed
narrators are mutually exclusive. Each successful narrative is preserved as a task artifact with
lineage to the fresh query result. `analysis.model_usage` records route/version/cache/cost/tokens/
latency without raw rows or prompts. A cache hit reports zero new provider tokens/cost.

Construct a `RoutedNarrator` with `NarrationSettings` and a trusted current authorization-version
resolver. Its cache snapshot binds query conditions, certified metric definition and freshly
computed result values; variable query timing is excluded. SQL, source authorization and validation
still run on every task, so changed source values invalidate the cached prose. Permission changes,
model revocation and expired budgets block cached results as well as new calls.

`LangChainRoutedProvider(model).invoke` is an async adapter suitable for the executor's provider map.
It requests structured `Narrative` output with a bounded output token parameter, requires actual
input/output token usage from the provider and fails closed on missing usage or invalid structure.
Use existing CC Switch model constructors to resolve credentials without copying secrets into Git.
Pin the model route to reviewed configuration and set transport deadlines/retries on the model.
Provider enrollment and review are trusted operator steps; the code never auto-approves a model.

## Feishu notification receiver (2026-10-19)

Supply a `FeishuNotificationBackend` for an explicit notification destination in
`BusinessActionRuntime(feishu_backends=...)`. `FeishuTarget` pins application, sender identity,
receiver type/ID and configuration version. The final persisted approval includes a `delivery_binding`
with these fields, the exact wire-content hash and configuration hash. Changing sender/recipient/
configuration after approval blocks execution. The normal owner/scope/switch/rate controls still apply.

`LarkCLITransport` resolves the installed CLI and uses argument arrays, explicit user/bot identity,
bounded timeouts and the documented `ok=true` service envelope. Credentials stay in the CLI's own
protected store. Error messages never include raw CLI diagnostics. Notification text has a stable
action/evidence marker; the 36-character action UUID is the receiver idempotency key. Feishu's
native duplicate window is limited, so uncertain/claimed actions are never automatically resent.

Before any send, current application/sender and admission are checked. The runtime reserves the
local destination and persists exact approval consumption before crossing the receiver boundary.
A send acknowledgement is saved as a receiver hint before readback. Completion requires matching
message ID, sender, conversation and exact text from a receiver read. If send/readback is uncertain,
`POST /v1/business/drafts/{action_id}/reconcile` accepts a `message_id` and only reads the receiver;
it never sends a replacement. Expired executing claims may be reconciled after process loss, but
an active claim cannot be overtaken. Other actions to an unresolved destination are blocked.

A separately approved compensation recalls only the original matching message. A second explicit
human confirmation port (`recall_confirmed`) is required for the CLI's high-risk recall operation;
there is no automatic `--yes` retry on exit 10. Receiver readback must verify the recalled/deleted
state. If a platform/CLI version cannot expose that evidence, compensation remains uncertain for
manual verification rather than claiming success. Real recall has not been exercised in this pilot.

`examples/feishu_notification_smoke.py --state-dir .state/feishu-pilot` prepares a local request to
the currently verified user's own account and prints the exact sender/recipient/content. Only after
that human confirms it may an operator add `--execute-confirmed`. The local requester is the pilot
service; the approval records the actual verified human ID, never invented independent reviewers.
`--reconcile-message-id` supports read-only recovery. The helper is explicitly a development live
smoke; it cannot provide production admission or the required independent multi-party qualification.

A user-authorized bot-to-self message was sent and read back successfully on 2026-10-19. Repeating
the completed action returned the same message ID and resource revision, with no second send.
No group message, recall, or GitHub PR was created. Private receiver IDs and runtime databases remain
under the ignored `.state/` directory; no bearer token/app secret was copied into the repository.

## Fresh periodic reports (2026-10-19)

A `ReportSchedule` can now pin a certified `query`. Supply a `MetricReportRefresher` backed by the
existing analysis workflow, plus its `read_artifact` method as the action runtime's `artifact_reader`.
Use separate protected databases for schedules, refresh metadata, approvals and action state.
Schedules without a query retain the earlier fixed-snapshot behavior.

Each due occurrence executes the certified read-only query and normal validation, then freezes the
complete delivery package as a `scheduled_report` artifact with source lineage. The generated draft
binds the actual report hash and `ArtifactRef`. Fixed query bounds are preserved; `lookback_minutes`
with an unbounded query creates a rolling window ending at the scheduled due time. Every occurrence
still needs its own exact action approval. This refreshes and generates reports but does not enable
unattended external publication or bypass the report receiver's future qualification.

Report generation runs outside the schedule transaction, so cancellation remains available during
query/model work. Scope, enabled state, token and expiry are rechecked before queueing a draft.
A generated occurrence is pinned before proposal: a failure while creating the approval does not
regenerate different content when the source changes. Concurrent requests cannot run the same
occurrence twice. A crashed `generating` record needs operator reconciliation; failed read-only
runs may retry. Artifact ownership, classification and actual bytes/hash are rechecked before
proposal and action execution; missing or modified report evidence blocks delivery.

Previously persisted local actions remain readable. Integrity and completion checks use the exact
stored approved JSON rather than materializing newly added optional schema fields into old hashes.

## Enforcing extension admission at assembly/use (2026-10-19)

`build_supervisor` now rejects new or modified specialist definitions unless an `ExtensionAdmission`,
actor and pinned `agent_versions` are supplied. Existing checked-in specialist definitions remain
the core baseline. The reviewed hash includes the full specialist definition. An admitted graph
rechecks version/scope/expiry/revocation before and after sync/async calls and during streaming.

`specialist_skills` applies reviewed `SkillDefinition` templates through the same factory. Skill
context is literal text; no Python formatting/attribute evaluation, filesystem import or arbitrary
code loading occurs. A skill may reduce its specialist's explicit tools, but cannot add tools absent
from that tool group. The skill is pinned and rechecked with its graph; altered prompts/tool lists
need a new reviewed version. Existing tool policy and source authorization remain mandatory.

`DataEngineeringRuntime` accepts the two checked-in read-only SQLite/DuckDB implementations as its
core baseline. A new connector implementation/subclass requires `admission`, a pinned
`connector_versions` entry and non-secret `connector_configurations`. The digest binds class and
method implementation source, current capabilities, source path/timeout and declared configuration.
The wrapper exposes only the read-only connector interface and carries no dynamic imports.

The policy gateway establishes a request-local actor context from the actual `ToolRequest`.
Every admitted connector native read checks that context, current registry state and unchanged
implementation/configuration before and after the read. Context is isolated across threads and
cleared on failure; calling an admitted connector directly outside the gateway fails closed.
Revocation blocks subsequent reads, including repeated aggregate queries within profiling. This
local enforcement is code-backed; production still requires independently authenticated reviews
and evidence, not synthetic test fixture enrollments.

## Measured optimization and complete acceptance (2026-10-19)

The aggregate runner now consumes `--optimization-evidence` with operator-pinned
`--routing-configuration-hash` and `--evaluation-dataset-hash`, plus `--stage4-release-bundle`.
Both reuse the operator-owned `--trusted-signers` registry. Optimization evidence binds exact
revision/environment/configuration/held-out dataset, at least 90 tasks, baseline and optimized
quality, measured billing, P95 latency, safety/cache-authorization/budget checks and positive
predeclared reduction targets. Quality and safety may not regress; cost and latency may not worsen.
Security and operations signatures must come from distinct identities and keys; proofs expire
within one day. Estimated prices or cached-token counters alone cannot qualify as measured billing.

Acceptance is computed from all six independently qualified action gates, optimization and the
existing Stage 4 release verifier, plus the clean-tree/local regression checks. With every valid
proof it can return `passed`; missing, stale, changed, untrusted or below-target evidence remains
`incomplete`. `--require-production` now exits according to those actual blockers rather than
always failing. This verifies admission evidence; it does not deploy or widen runtime permissions.
The real environment remains incomplete: this development run has a single authorized Feishu
connectivity sample, not the independent multi-party production evidence those gates require.

A local SDK integration test also found that invocation kwargs are dropped by LangChain's raw
structured-output `RunnableMap` branch. The provider bridge now binds the output-token cap directly
on the model's forced Narrative tool request. A test using the installed ChatAnthropic runnable
verifies the cap reaches the model call, in addition to the existing returned-usage bound checks.

## Background report worker

`generaldataagent.business.worker.ReportWorker` provides a bounded durable polling loop over
`ReportScheduler`. Register an existing owned schedule with a future delegation expiry (at most
30 days), then run `worker.run(stop_event, on_result=operator_status_sink)`. The worker database
must be protected like the schedule and approval databases and kept outside agent-visible storage.

Deployment must inject `token_provider(workload) -> new_short_lived_token` using its authenticated
workload identity and a revocable grant for that exact schedule hash/owner/project/domains/expiry.
The provider is a trusted operator port, not an agent callback or public API. Never reuse a user's
session or persist bearer/refresh credentials. It must return a fresh broker session with exactly
`business:propose`, exactly the workload domains and owner, and `not_after=workload.expires_at`.
The worker checks these constraints, rechecks schedule configuration, and revokes the session after
every tick. Service-provider grant revocation must also revoke any outstanding broker session.
A deployment identity provider and process manager still need operator configuration.

Each tick handles at most one occurrence per claim; SQLite leases coordinate multiple pollers.
Leases expire after 900 seconds following a crash, while deterministic occurrence IDs and the
scheduler's own transaction prevent duplicate proposals. Existing report-generation quarantine
still requires reconciliation after an interrupted generation; the worker does not bypass it.
Provider failures emit exception types only, retry after the polling interval, and never renew a
stored token. Expired or completed jobs are disabled. Stop the process gracefully via its stop event;
`worker.cancel(schedule_id, owner_token)` also cancels the schedule, including in-flight refresh.
This worker only proposes drafts. Approval, delivery and compensation use their existing separate
permissions and exact approvals. No message or downstream change is sent by polling.

## Metered Supervisor and specialist models

Pass an opt-in `OrchestrationRouter` as `MetricSupervisorRuntime(orchestration_router=...)` to
route every Supervisor/specialist chat turn through current capability admission, authorization,
a shared durable token/cost/call budget and a transport deadline. Supply explicit `Budget`,
`NarrationSettings`, audit sink, fresh authorization/version callbacks and `enabled` switch.
The default switch denies execution. Model routes need actual independent quality/security
admission using `OrchestrationModelConfig.fingerprint`; the default HTTPS endpoint, chosen endpoint,
model name, deadline and retry/streaming policy are bound into that fingerprint. Credentials remain
in the provider's protected environment. Environment endpoint variables cannot redirect a route.

A deployment routes within one native provider family (Anthropic or OpenAI). Clients stay native,
so the existing filesystem/shell/general-purpose-subagent exclusions remain active. The factory
also excludes automatic summarization, which otherwise makes unmetered model calls outside agent
middleware. Large histories fail their bounded envelope instead. Text and native tool-use messages
are supported; image/file content is refused. Actual tool-call messages are returned intact and
are not cached. Framework structured-output retries and specialist calls are charged individually;
SDK retries and native/global LangChain response caches are disabled, and transport failures do
not trigger automatic route fallback.

The reservation includes a conservative serialized-byte input allowance (messages, system prompt,
tools, response schema and framing) plus the enforced native output cap. Configure the task budget
for all anticipated turns: it is deliberately more conservative than a provider tokenizer. Failed
calls keep their reservation. Reported provider input/output usage is mandatory and checked against
the reservation, recorded in audit without prompts/results, and rechecked against authorization,
model admission and task deadline before the response is accepted. The same actor/thread always
reuses its task budget across retries and restarts; changing a query does not reset that budget.
Child certified analysis/narration retains its separately registered deterministic workflow budget.

Offline tests exercise native Anthropic and OpenAI agent graphs, including a real specialist
handoff and the exact cap reaching the provider invocation boundary. No real model route has been
enrolled or billed by these tests. A held-out real billing/latency benchmark and qualified operator
configuration remain required before enabling this routing in production.

### Recovering report versions after missed periods

For schedules which should continue after a missed or cancelled occurrence, explicitly set
`ReportSchedule(revision_policy="current_at_proposal", ...)`. Each new occurrence captures the
current destination resource revision before creating its own approval. If no resource exists it
uses the configured initial revision. Already persisted proposals keep their original revision,
content and approval after a scheduler restart or lost acknowledgment, even if another action
has since changed the resource. Execution still rejects any intervening resource change; a stale
approval never rebases itself. The default `sequential` policy preserves the original strict
`initial_revision + occurrence_index` behavior for schedules which require every prior occurrence.

## Feishu report publication

An explicitly configured `FeishuReportBackend` can publish a generated `ReportDraft` through the
existing `feishu_backends={("report", destination): backend}` runtime port. Add a matching enabled
report `ActionControl` and use `MetricReportRefresher.read_artifact` as the scoped artifact reader.
Its target, transport, admission check and recall confirmation port work like the notification
backend. No destination or credential is embedded in the implementation or enabled by default.

The report must have a registered frozen `scheduled_report` artifact. Proposal reads it under the
requester's identity, checks actual bytes/classification/hash, validates the strict snapshot and
its evidence task/lineage/classification, and renders summary, complete table, claims/citations,
insights and limitations. `payload.publication_text` contains the full exact plain-text preview;
`delivery_binding` pins recipient/sender/configuration/final wire hash. Human review must cover
this text and its final action/evidence footer, recipient and sender identity. Approval impact
metadata identifies Feishu delivery rather than describing it as a local draft.

The renderer accepts at most 12,000 characters and 18,000 UTF-8 bytes before the short footer.
Oversized reports fail before approval; no implicit truncation, attachments or multi-message split
is performed. Explicit plain-text message type avoids Markdown image downloads/rewrites, and
report mention tags are refused. Internal artifact references are evidence identifiers, not public
links. This adapter publishes report content in IM; it does not create a Feishu cloud document.

Before consuming an approval and again immediately before the external write, the runtime rereads
the scoped artifact and rederives the approved text. After readback it checks references again.
Receiver hints, exact readback, uncertainty quarantine, no automatic resend, restart receipt replay,
read-only reconciliation and independently approved/explicitly confirmed recall remain in force.
Reconciliation also requires the source artifact to remain available and authorized.

All report-delivery/recall tests use isolated fake receivers with actual local report generation.
No new real send or recall is authorized by previous notification smoke consent. Live publication
still requires an exact human-reviewed report, sender and recipient, and independent qualification
is separate from a successful message smoke test.
