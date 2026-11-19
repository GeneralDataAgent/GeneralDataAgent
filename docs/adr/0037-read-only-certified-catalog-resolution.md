# ADR 0037: Resolve against the certified catalog without starting work

Status: Accepted for a local development adapter, 2026-11-19.

## Context

`POST /v1/analysis/questions` both resolves a natural-language question and executes a certified
metric end to end. `POST /v1/analysis/tasks/questions` resolves and then durably accepts a query.
A client that only needs to know which certified metric a question identifies, or whether the
catalog entry is ambiguous or absent, has no path that stops before execution. Reusing the
executing endpoints for discovery would create tasks, artifacts, budget charges and model work as
a side effect of a lookup.

The deterministic resolver already filters by the actor's data domains, so a read-only projection
can be exposed without a second matching implementation. The risk is that a discovery endpoint
could be mistaken for certification or execution, or could disclose catalog entries outside the
caller's domains.

## Decision

Add `POST /v1/analysis/metrics/resolve` with `CertifiedMetricResolutionRequest`: a `question` and
an optional `metric_version`, and no dimensions or time range. The session broker must authenticate
the caller and grant `analysis:run` before resolution. Resolution is narrowed to the `x-data-domain`
values declared on the request, which the identity middleware already requires to be a subset of
the session's authorized domains.

The response is `CertifiedMetricResolution` v1 with the same `resolved`, `ambiguous` and
`not_found` semantics as question acceptance, bounded metric candidates, and the existing
clarification text. It does not echo or persist the question. Fixed boundary markers state
`scope=certified-metric-catalog-read-only`, `execution=not-started`, and
`task_created`, `query_compiled`, `artifacts_created`, `budget_charged` and `model_invoked` all
`false`.

Assembly requires the resolver and the metric runtime to share the exact catalog instance. The
endpoint is rejected in production mode with the other local adapters.

## Consequences

Clients can explicitly discover authorized certified metrics and inspect ambiguity before
composing a query, without task, queue, checkpoint, audit, artifact or budget side effects. The
resolver reuses `catalog.search` and `resolve_question`, so it cannot diverge from the acceptance
paths.

This is not query certification: dimensions and time ranges are rejected at this boundary rather
than validated against the metric. It is not execution, delivery evidence, an independent review
or production qualification. The question is not audited because nothing executes and no budget is
charged; callers that need a governed record must use the durable question path. A later catalog
change affects future resolutions but does not rewrite any accepted query.
