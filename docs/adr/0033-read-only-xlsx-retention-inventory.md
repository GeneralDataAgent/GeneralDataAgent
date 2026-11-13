# ADR 0033: Inventory retained private XLSX capacity read-only before any cleanup

Status: accepted for opt-in local development; production admission unchanged.

## Context

Private XLSX staging hides expired files from preview, confirmation, derivation, listing and value
pages, but their encrypted rows and source bytes remain in the dedicated SQLite database and keep
counting toward the 20-per-creator and 200-total limits. An operator has no way to review retained
capacity, expiry, release compatibility or whether a retained snapshot still matches the current
declaration. Deleting rows from an ad-hoc script would bypass record decryption and binding checks.

## Decision

Add an independent operator port that opens one explicitly configured intake database with SQLite
`mode=ro`. It refuses to create missing paths, schema or parents, verifies the stored key canary,
`user_version` and exact table layouts, and decodes every retained record with the released
record-binding checks. It never parses a workbook, re-derives or re-scans values, writes audit or
changes the store.

Emit a bounded v1 `XlsxMaintenancePlan` with file/task IDs, truncated non-reversible keyed owner
references, staged/confirmed status, release compatibility, declaration presence, snapshot state
(`none`/`current`/`stale`), snapshot hash, retained row/cell counts and duplicate-row count, source
hash and bytes, creation/expiry times and a review state. Report per-creator retained/expired counts
and bytes plus configured capacity and headroom. Emit fixed facts `values=not-read-by-this-inventory`,
`deletes=false`, `releases_capacity=false` and `audit=not-emitted`.

Bind immutable retained facts into `inventory_digest`; exclude wall-clock expiry and snapshot
staleness so repeated reads are stable while a new upload, confirmation, declaration or snapshot is
detectable. Corrupt records, wrong keys, unsupported layout or more rows than the configured bound
reject the whole observation rather than producing a partial plan.

## Consequences

An operator can review expired capacity and stale snapshots without mutating state. The plan is a
review snapshot, never a delete authorization; a later cleanup slice needs exact one-time approval,
content-free tombstones, idempotent replay and crash recovery, and must not claim safe erasure. The
CLI is local and operator authorized, not a browser, model or tenant-management endpoint. Default-off
development assembly and the `independent_review=not_run` / `production_accepted=false` boundaries
remain unchanged.
