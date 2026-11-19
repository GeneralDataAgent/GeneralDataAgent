# ADR 0038: Download only same-task evidence artifacts

Status: Accepted for the local task adapter, 2026-11-19.

## Context

Completed result reads expose evidence references, but consumers could only see metadata. A direct
artifact-store route would read by opaque URI and could serve unreferenced, other-task or other-scope
objects. The result already carries table, chart and narrative content; a download route adds the
original evidence bytes for audit and troubleshooting without turning artifact identifiers into
public links.

## Decision

Add `GET /v1/analysis/tasks/{task_id}/artifacts/{artifact_id}`. The handler runs the full completed
result check first: creator/scope authorization, protected checkpoint consistency, fixed delivery
reconstruction, and exact same-task evidence binding with content hash and size verification.
Governed stores keep their export authorization and DLP scan. The requested identifier must match
exactly one reference in the validated `DeliveryPackage.evidence`; otherwise it is indistinguishable
from an unknown or unauthorized artifact and returns 404.

Only the evidence artifact types already admitted by result reads are downloadable (`metric_query_result`,
`chart_spec`, `llm_narrative`). Uploaded CSV/XLSX sources, arbitrary stored objects and unvalidated
work products are not reachable through this route. Responses are JSON attachments with
`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.

A successful download emits one bounded `artifact.downloaded` audit event containing only the
artifact type and byte size. A missing or failing audit sink denies the download rather than
returning bytes unaudited. Authorization is rechecked after the read, before the response.

## Consequences

Consumers can retrieve the exact evidence bytes that produced a completed delivery without invoking
the graph, rerunning SQL, reserving budget, calling a model or changing task attempts. The route does
not list artifacts, accept a URI, serve uploaded source files or add a general content-addressed
store API. It remains a local adapter, not a production download qualification; retention still
applies independently to the queue, checkpoints and artifact store.
