# ADR 0031: Resolve questions before durable task acceptance

Status: Accepted for a local development adapter, 2026-11-11.

## Context

`POST /v1/analysis/questions` resolves a natural-language question and executes synchronously, so
the caller has no durable task before analysis returns. `POST /v1/analysis/tasks` accepts only a
structured `MetricQuery`. Persisting the question body would retain ungoverned text, and resolving
inside the worker would let an ambiguous or unauthorized request occupy the queue before failure.

## Decision

Add `POST /v1/analysis/tasks/questions`. It authenticates the session and `analysis:run` permission,
then resolves the question deterministically against the actor's authorized data domains before
acceptance. Only exactly one certified metric may proceed. Ambiguity and absence return HTTP 409
with the resolution status and metric candidates, never the question body.

The durable payload is the resolved `MetricQuery`. The question text is not stored in the queue,
event journal, audit or response. The same shared resolution and query-certification functions are
used by the synchronous endpoint and the durable path, and uncertified dimensions or time ranges are
rejected before a task exists. Idempotency covers the resolved query, so the same key with a changed
question that resolves differently conflicts, while different wording that resolves to the same
certified query is the same request.

The endpoint is rejected by production assembly with the rest of the local task adapter. Execution,
evidence, cancellation, retry and result reads reuse the existing task service.

## Consequences

Clients can now accept a question durably without retaining its text and can distinguish explicit
ambiguity from absence before work starts. Resolution is bound to the catalog at acceptance; a later
catalog change does not rewrite the accepted query, and execution still validates the certified
definition. This does not provide contextual follow-up, conversational memory, free-text execution,
model reasoning, UI, or production qualification. Cross-store atomicity and exactly-once effects
remain unclaimed.
