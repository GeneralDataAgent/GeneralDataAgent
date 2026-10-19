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
