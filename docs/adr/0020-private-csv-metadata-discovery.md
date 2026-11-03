# ADR 0020: Discover private CSV snapshots without replaying intake

Status: accepted for opt-in local development; production admission unchanged.

## Context

Stage 13.1A has durable private files and repeatable result reads, but a refreshed client cannot
discover its file IDs. Re-uploading to rebuild a workbench inventory risks duplicate source records
and does not expose honest expiry or processing compatibility. A real consumer is the existing
ASGI CSV demo reopened under a fresh session, ahead of a separate workbench project.

## Decision

Add an independent read-only metadata listing service and v1 summary/page contracts. Reuse the
encrypted store and current session guard. Freeze ordered membership in an encrypted, scope/page-
size/release-bound cursor with a fixed five-minute TTL, while rechecking current grant, per-file
expiry and registered status on every page. Do not migrate the CSV database or change its source/
profile contracts or processing implementation. New listing code alone does not invalidate the
existing loaded processing fingerprint.

Keep headers, rows, source bytes, paths and artifact bodies out of this response. Listing at most
20 retained scoped rows needs no CSV parsing, profile tool calls or new execution allowance. Emit
metadata-only audit and fail closed on corruption/audit failure. Show release incompatibility as
metadata rather than silently reinterpret an accepted source. A registered `profiled` status is
separate from actual report/evidence verification at the profile endpoint.

## Consequences

Refresh can discover and select real core file IDs without re-uploading. Pagination survives a new
session, preserves membership across insertions, omits expired/missing files and may return short
pages; statuses are not an immutable cross-page snapshot. Cursor replay is a bounded authorized
read, not a one-time mutation. Expired source rows still count toward retention quota. Raw cleanup,
tombstones, legal-hold/orphan reconciliation, sharing, numeric/XLSX conversion, browser UI, IM entry
points and production storage/SLO qualification remain separate work. Default-off assembly and
production constructor rejection remain in force.
