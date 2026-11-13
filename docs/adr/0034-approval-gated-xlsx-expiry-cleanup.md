# ADR 0034: Remove selected expired private XLSX rows only under one exact approval

Status: accepted for opt-in local development; production admission unchanged.

## Context

ADR 0033 added a read-only inventory of retained and expired private XLSX rows. Expired rows still
count toward the 20-per-creator and 200-total limits, and the inventory deliberately is not a delete
authorization. Deleting rows with an ad-hoc script would bypass record decryption, key canary checks
and store layout checks, and could leave capacity accounting or tombstones inconsistent.

## Decision

Add a separate local operator port, `XlsxSourceCleanup`, backed by the same released decoder and a
new `xlsx.maintenance.remove` policy rule. The rule allows only `remove-expired-source` for the
`data_engineer` role, requires approval and carries audit, approval, tombstone and retention
obligations. The port is not exposed through an API route, browser action, model tool or tenant
management surface.

Require one exact approval whose resource is the reviewed `inventory_digest` and whose arguments are
exactly that digest plus a sorted, unique file ID list. Re-observe the inventory before consuming the
grant; refuse drift, non-expired or unknown file IDs, wrong tool/action, unapproved requests and
malformed selections. Consume the one-time grant only after those checks.

Inside one SQLite `BEGIN IMMEDIATE` transaction, verify `user_version`, exact key/upload table layouts
and the key canary, verify or create the content-free `xlsx_source_tombstones` table, and for each
selected file write one tombstone and delete exactly that `xlsx_uploads` row. Roll back both on any
error. A tombstone binds file/task IDs, the truncated keyed owner reference, status, snapshot hash,
source hash and bytes, approval ID and inventory digest, and contains no header, value, formula,
idempotency key or key material. The derived snapshot shares the deleted row and disappears with it.

Emit a receipt with per-item `removed`/`already_removed` outcomes, reclaimed and remaining source
bytes, and fixed `evidence_artifacts_removed=false`, `safe_erasure=false`,
`destructive_actions_performed=true`. Exclude outcomes and times from the receipt digest so an
idempotent replay after a successful removal returns the same digest and never deletes twice. When
the inventory drifted before any removal, require a matching tombstone; otherwise refuse.

## Consequences

An operator can reclaim explicit expired capacity without bypassing released bindings, and each
removal is attributable to one exact approval. The port remains local and operator authorized. Free
pages, WAL files and backups may still contain removed bytes, so this is not safe erasure, does not
release a legal hold and makes no compliance-retention claim. Default-off development assembly and
the `independent_review=not_run` / `production_accepted=false` boundaries remain unchanged.
