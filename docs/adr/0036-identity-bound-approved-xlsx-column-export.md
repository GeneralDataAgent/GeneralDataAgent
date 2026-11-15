# ADR 0036: Export one approved bounded XLSX column subset through a local operator port

Status: accepted for opt-in local development; production admission unchanged.

## Context

The creator-private XLSX service retains canonical readings for declared columns inside an encrypted
payload. A bounded API value page can show those readings to the creator, but it cannot create a CSV
file, select an arbitrary declared-column subset or provide an attributable egress receipt. Reusing
the value page as an export would weaken the distinction between a read under current authorization
and a new operator-owned output. An ad-hoc script that decrypts the store could bypass the released
decoder, exact identity scope, approval binding, DLP re-scan, audit and formula-injection controls.

The approval registry previously recorded grant timestamps from the contract's real-time default
while the store used an injected clock for expiry checks. Deterministic consumers could therefore
see a grant whose `approved_at` disagreed with the configured clock. A local export that compares
grant time needs one clock source.

## Decision

Add `datasets/xlsx_export.py`, a local operator CLI with no API route, browser action, model tool or
tenant-management surface. Add an R2 `xlsx.export.columns` policy rule that allows only
`export-declared-columns` for `data_engineer`, requires approval and carries audit, approval, DLP,
formula-injection and retention obligations. The operator supplies one existing encrypted store,
a private key file, one exact one-time approval ID, a shared local session database, a private opaque
token file, an append-only local audit path, explicit actor/domain scope, a retained file and
snapshot hash, an instance ID, sorted selected column indices, a bounded keyset row range and one new
output path.

Bind the approval to the full canonical `ToolRequest` fingerprint: explicit user/tenant/project,
role, domains, tool, action, resource and every argument. A deterministic `export_hash` over the
file, snapshot hash, selected columns, keyset range and instance ID is the resource. Recheck both
the approval request and grant fingerprints, the exact argument dictionary, classification, domains,
expiry, disposition and consumption state. Verify the live session reference on every checkpoint,
using a monotonic bounded deadline; refuse business constraints, missing `analysis:run`, mismatched
identity or lost domain access. Use the approval store's injected clock for `approved_at` so grant
time and expiry share one deterministic source.

Open the existing database with SQLite `mode=ro`; verify `user_version=1`, exact table layouts and
the AES-256-GCM key canary; reuse `XlsxUploadStore.decode` through the read-only view. Verify exact
HMAC creator scope, decrypted actor equality, unexpired retention, confirmed status, source hash
and byte count, released parser/processing fingerprints, source/preview/confirmation/interpretation
snapshot bindings and selected headers against both the declaration and snapshot. Recompute the
released whole-reading digest from every retained status and canonical value before projecting a
subset; a tampered projection fails closed. Do not re-parse the workbook, trust formula caches,
write a source row, register an artifact or create a `DatasetSnapshot`.

Write CSV with only the selected labels and canonical `value` readings; missing, unparsed and
not-trusted readings stay empty. Neutralize formula prefixes `=+-@` after optional whitespace
and leading tab, carriage return or line feed with an apostrophe. Bound the page to 500 rows
and 2 MiB. Re-scan the entire exact CSV output,
including labels and CSV escapes, with `TextDLPScanner.check_export` at `INTERNAL` classification.
Emit a content-free `xlsx.columns_exported` event with `phase=prepared`, the approval ID, hashes
and counts before consuming the grant. Audit failure denies the export. After audit, check the current retained
snapshot, declaration and live identity, then consume against the exact tool request. The CLI writes
and syncs a private `0600` staging file before
consumption, then publishes it as a new file with an atomic no-overwrite link. Key and token files
must be operator-owned private regular files. Pass the opaque token through its file.

## Consequences

An operator can produce one attributable CSV for an explicitly approved declared-column subset
without widening the creator-private API. The output is an operator-owned file outside the artifact
index, not an analysis input, content-verification result, `DatasetSnapshot`, independently reviewed
export or production egress path. DLP denial, drift, audit failure, session revocation or staging
failure before consumption leaves the grant unconsumed. On a late destination failure the CLI
preserves the private staged CSV and reports its path and SHA-256 with the consumed approval ID.
The operator can recover those bytes after verifying the digest. A process crash may leave a
private staging file with no diagnostic.
Default-off development assembly and the `independent_review=not_run` /
`production_accepted=false` boundaries remain unchanged.
