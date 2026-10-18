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
