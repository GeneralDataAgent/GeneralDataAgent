# ADR 0007: Approval-bound business drafts

Status: accepted for local development; external action qualification pending.

Stage 5 adds six independently configured destinations: Dashboard, ticket, scheduling PR,
model-registration application, enterprise-IM message and periodic report. These are strict
structured drafts. The installed backend is a transactional local outbox, with no external
credentials, arbitrary commands, deployment or message transmission.

Each action binds original task/thread, content hash, actor scope, idempotency key, destination,
expected resource revision, expiry and exact expiring approval. Independent owner/domain/switch/rate
controls are trusted operator configuration. No HTTP endpoint can approve an action. The existing
approval service enforces separation of requester and approver. Execution checks current identity,
policy, DLP, switches, rate and resource revision before claiming and repeats identity/policy/resource
checks before committing the local receipt. Read and propose are separate explicit permissions.

A durable claim precedes approval consumption. Interruption during that boundary is quarantined as
executing/uncertain for manual reconciliation; it is never replayed with a consumed approval.
Completed retries return the same receipt. Local resource mutation, receipt and chained completion
event commit together. Cancellation closes an unconsumed approval. Compensation is a new exact
approval and only retracts the currently matching draft revision; it cannot reverse a later update.

Protected approval and action databases must be separate. Hash chains detect edited intermediate
history but require independent signed checkpoints to detect whole-history rollback. Local state is
not a substitute for the Stage 4 independent audit infrastructure. A future external adapter must
prove receiver-side idempotency, reconciliation, compensation, native authorization and independently
verified delivery receipts before it can be enabled. A local draft receipt never claims delivery.
