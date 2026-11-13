# ADR 0035: List declared XLSX intent as metadata only

Status: accepted for opt-in local development; production admission unchanged.

## Context

The creator-private XLSX service can record a header row, per-column type, missing-value tokens and
duplicate-row policy, but the only cross-file view is the file metadata page. It reports that a
declaration exists without exposing which columns were declared, their types, the missing-token
counts, the interpretation hash or whether a retained snapshot still matches. A caller therefore has
to open each file individually, and the server has no single bounded surface that proves what it would
consider declared without also returning content.

Returning header labels, missing tokens or stored values would widen the disclosure boundary beyond
what the declaration listing needs. Re-parsing workbooks or re-deriving readings would also turn a
metadata read into a new extraction path.

## Decision

Add `GET /v1/analysis/files/xlsx/declarations`, registered before the `/{file_id}` route. It returns
at most 20 items — the hard per-creator retention bound — ordered by creation time and file ID.

Each item exposes only binding facts: file/task IDs, fixed `status=confirmed`, release
`availability`, sheet name, header row, duplicate policy, `interpretation_hash` and snapshot state
(`none`/`current`/`stale`). Each declared column is reduced to its index, declared type,
empty-is-missing flag and count of missing tokens. Header labels, token strings, cell values, formulas,
row samples and idempotency keys are never returned. The call reuses the stored encrypted payload and
released record-binding checks; it does not parse the workbook, re-preview it, re-derive readings or
write any row.

Include only confirmed files with a recorded declaration whose current expiry is in the future, and
only records inside the caller's exact creator/tenant/project/domain scope. Recheck authorization while
decoding every candidate row. A snapshot is `stale` when its bound interpretation hash differs from the
current declaration. Emit `xlsx.declarations_listed` with only `item_count` before returning; audit
failure returns no page. More rows than the hard bound, an unreadable record, an unsupported layout or a
corrupt store fails the whole listing closed. The page states `declarations=metadata-only`,
`headers=not-returned`, `tokens=counts-only` and `values=not-returned`.

## Consequences

A caller can review which declarations are retained, what types and missing policies they bind and
whether a derived snapshot is current without opening each file or receiving workbook content. This is
a declaration-metadata read, not an export, profile, analysis input, `DatasetSnapshot`, artifact or
content-verification claim. It adds no write path, model tool, browser action or tenant-management
surface. Declaration revision still happens through the existing interpretation endpoint. Default-off
development assembly and the `independent_review=not_run` / `production_accepted=false` boundaries
remain unchanged.
