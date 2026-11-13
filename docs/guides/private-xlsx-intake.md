# Private XLSX staging, pinned preview and declared interpretation

The opt-in local XLSX service accepts workbook **bytes**, applies the bounded structural preflight and
inert text preview, then stores an encrypted preview bound to the exact source, parser, configuration
and limits. An explicit confirmation can pin that preview. A separate declaration can then record the
header row, per-column type, missing-value tokens and duplicate-row policy as **intent only**. A
read-only resolution can then derive deterministic per-cell readings for the declared columns over the
same sampled window, and a bounded conformance report can read every present row below the header for
those columns while returning only counts, reasons and digests. A further read-only report counts the
number formats (`cellXfs`/`numFmts`) used by the declared columns without applying them. A retained
derived snapshot can then persist the canonical values of the declared columns, encrypted, and expose
counts and hashes plus a bounded, explicitly requested page of those declared values. A separate local
operator inventory observes retained and expired files and capacity headroom read-only, without
deleting anything; a second local operator port can then consume one exact approval to remove selected
expired rows and leave a content-free tombstone. None of these calls returns the workbook's undeclared
content, extracts values into an analysis surface, profiles data, evaluates formulas, converts
currencies or percentages, rewrites stored values or makes model calls. Production API assembly
rejects this adapter.

This is the thirteenth slice of XLSX ingestion. The service can now derive whole-sheet conformance,
inert number-format facts and a retained encrypted snapshot of declared-column values, read those
values back page by page without creating an artifact, observe retained capacity through a local
read-only operator inventory, and perform an exactly approved removal of selected expired rows.
Analysis, `DatasetSnapshot`, automatic expiry cleanup and production qualification are not
implemented.

## Assembly and API

Construct `XlsxUploadStore` with a dedicated SQLite path and an operator-provided 32-byte AES key, and
`XlsxIntakeService` with the existing session broker, audit sink, optional DLP scanner and
`XlsxLimits`. Keep the database and key outside agent-accessible content. Pass
`create_app(session_broker=broker, xlsx_intake=service)`; the identity brokers must match. Default
routes return 503.

Existing bearer/identity/domain headers and `analysis:run` apply. File scope is the exact
creator/tenant/project and normalized domain view; broader or narrower headers select another view,
not a share. No actor, host path, file name, connector or artifact-root configuration is accepted.

| Endpoint | Behavior |
|---|---|
| `POST /v1/analysis/files/xlsx?sheet=Name` | Raw OOXML workbook bytes, `Content-Encoding: identity`, `Idempotency-Key`; 201 private staged preview |
| `GET /v1/analysis/files/xlsx` | Creator-private metadata page over non-expired staged files; no headers, values or hashes |
| `GET /v1/analysis/files/xlsx/{file_id}` | Current-grant preview of the fixed staged snapshot; the sheet pinned at staging is used |
| `POST /v1/analysis/files/xlsx/{file_id}/confirm` | Strict JSON with `confirmed: true` and the exact source/preview hashes; pins a confirmed preview |
| `POST /v1/analysis/files/xlsx/{file_id}/interpretation` | Strict JSON declaration over a confirmed preview; records intent without rewriting values |
| `GET /v1/analysis/files/xlsx/{file_id}/interpretation` | Derived, read-only per-cell readings of the declared columns; nothing is stored |
| `GET /v1/analysis/files/xlsx/{file_id}/conformance` | Derived whole-sheet counts, reasons and digests for the declared columns; no cell text is returned |
| `GET /v1/analysis/files/xlsx/{file_id}/formats` | Derived whole-sheet number-format counts for the declared columns; format codes stay inert |
| `POST /v1/analysis/files/xlsx/{file_id}/snapshot` | Derive and retain canonical declared-column values inside the encrypted payload; returns counts and hashes only |
| `GET /v1/analysis/files/xlsx/{file_id}/snapshot` | Stored snapshot metadata with `status=current`/`stale`; never returns a value |
| `GET /v1/analysis/files/xlsx/{file_id}/snapshot/values` | Keyset page of retained declared-column values for the creator; `current` snapshots only, re-DLP-scanned and audited |

`sheet` is optional at staging and defaults to the workbook's first sheet. An unknown or invalid
sheet is rejected. Later reads ignore a `sheet` query parameter; restage under a new idempotency key
to preview a different sheet.

The request must use the exact media type
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`; generic `application/vnd.ms-excel`
and compressed transfer encodings are rejected.

Same scope/key/source/config/preflight returns the same file and does not extend expiry. Changed input
under the same key returns 409; new input needs a new key/file ID. IDs are server generated. This is
not a task submission endpoint and creates no analysis task.

## Metadata listing

`GET /v1/analysis/files/xlsx` returns at most 20 metadata items per page (default 10). Each item
carries only the file/task ID, `status` (`staged`/`confirmed`), `availability`
(`compatible`/`release_incompatible`), the pinned sheet name, row/column counts, byte count,
`has_interpretation`, and created/expiry timestamps. It never returns headers, cell values, formulas,
hashes or idempotency keys, and it does not re-parse the workbook.

The listing uses the same current-grant rules as a preview read: exact creator/tenant/project and
normalized domain scope, rechecked on every page, so another identity sees its own inventory only.
Expired rows are skipped. `availability` reports whether the stored parser/processing fingerprints
still match the running release; a `release_incompatible` row remains listable but preview/confirm/
interpretation continue to fail closed with restage-required, and listing never reinterprets it.

`next_cursor` is opaque, encrypted and bound to the scope, page size and listing release. It freezes
first-page membership and order for at most five minutes; each later page rechecks authorization and
expiry. An altered, cross-scope, release-changed or expired cursor returns
`409 xlsx_list_cursor_invalid_or_expired`. There is no `total`, filename, column name, sample or
cross-store snapshot. Audit records `xlsx.files_listed` with only `item_count`/`has_more`; audit
failure returns no page. Declaration listing across files is not implemented.

## Staged preview

The response contains a `file` record, the structural preflight report and the inert text preview.
Preview semantics are fixed:

- values are text only (`values=inert-text-no-conversion`);
- formulas are separate text with a cached-text flag, never evaluated;
- dates remain serial text; currencies and percentages are not interpreted;
- the first present row is treated as the preview header;
- `row_numbers` lists the absolute worksheet row of every materialized preview row, so a declaration
  can select one of those rows without re-reading the workbook;
- empty/duplicate headers, row/column truncation and formulas inside the preview window are reported.

Preview limits are 5 data rows and 64 columns, at most 32,768 characters per cell and 65,536
characters in total. All limits reject rather than truncate a cell or return partial content.

## Explicit confirmation

Confirmation changes only the staged record status and pins a confirmation digest. It does not
itself select a header row, infer a column type, convert dates/currencies, profile values or run
analysis; those choices, when recorded at all, live in the separate declaration described below.

```json
{
  "schema_version": "1.0",
  "confirmed": true,
  "source_hash": "sha256:<64 lowercase hex digits from preview>",
  "preview_hash": "sha256:<64 lowercase hex digits from preview>"
}
```

The request must use `Content-Type: application/json` and is limited to 1,024 bytes. `confirmed` is a
strict boolean and must be `true`; unknown fields, non-boolean values or `false` are rejected with 422.
Both hashes must match the staged record exactly; a changed source or preview hash returns 409.

Confirmation re-runs the current parser and preview binding before writing. Parser/limit drift returns
409 (`xlsx_parser_or_limits_changed_restage_required`); a different preview result returns 404. On
success the record becomes `status=confirmed` with a deterministic `confirmation_hash` over the file,
source, preview, parser/processing fingerprints, config, preflight digest and sheet, plus
`confirmed_at`. It does not extend expiry and does not change the preview. Replaying the same request
returns the same confirmed record; restaging with the same idempotency key also returns the confirmed
record rather than resetting it. Audit is emitted before the encrypted body update, and an audit
failure leaves the file staged.

## Declared interpretation (never applied)

A declaration records what the user intends the preview to mean. It requires `status=confirmed` and
the exact source, preview and confirmation hashes from that confirmed record; otherwise it returns 409.
The preview, source bytes and confirmation stay unchanged, and the declaration is not applied to any
value, report or analysis.

```json
{
  "schema_version": "1.0",
  "source_hash": "sha256:<64 lowercase hex digits from preview>",
  "preview_hash": "sha256:<64 lowercase hex digits from preview>",
  "confirmation_hash": "sha256:<64 lowercase hex digits from confirmed file>",
  "header_row": 1,
  "columns": [
    {
      "index": 0,
      "header": "<exact preview header text at that index>",
      "type": "text",
      "empty_is_missing": true,
      "missing_tokens": ["NA"]
    }
  ],
  "duplicate_rows": "reject"
}
```

- `header_row` must be one of the preview's absolute `row_numbers`. The supported rows are therefore
  limited to the materialized preview window; a row outside it returns
  `xlsx_header_row_outside_preview`.
- each `index` is unique, below the declared header row's width, and `header` must equal that label
  exactly; duplicate indices or a mismatched label are rejected. Empty or duplicate preview headers
  are still declarable by index.
- `type` is limited to `text`, `integer`, `decimal`, `boolean` or `date`. `datetime`, `currency` and
  percentage declarations are rejected; a `date` declaration means an Excel date serial, not a
  formatted datetime string.
- `empty_is_missing` defaults to `true`; up to 8 literal `missing_tokens` of at most 32 characters
  are recorded. Derived readings match tokens by exact text only; no trimming, case folding or
  normalization is applied.
- `duplicate_rows` is explicitly required: `reject` or `keep-all`. It is recorded, not executed.

The request must use `Content-Type: application/json` and is limited to 65,536 bytes. The resulting
`interpretation` carries `values=declared-only-no-conversion` and a deterministic
`interpretation_hash` over the source/preview/confirmation hashes, header row, column declarations and
duplicate policy. Replaying the identical declaration returns the same interpretation without writing
or auditing again; a different declaration replaces it, emits a new `xlsx.interpretation_intent`
audit event before the encrypted body update, and returns the new hash. An audit failure leaves the
previous interpretation (or none) in place. Listing or selecting these declarations across files is
not implemented.

## Derived interpretation (read-only)

`GET /v1/analysis/files/xlsx/{file_id}/interpretation` requires a recorded declaration and re-runs
the same parser and binding checks as a preview read. It returns per-cell readings for exactly the
declared columns and the materialized preview rows after the declared header row. Nothing is written,
persisted, audited or added to the preview, and repeated reads return the same result.

Each cell carries its absolute `row`, the unchanged `raw` preview text, its factual `kind` (`empty`,
`number`, `text`, `boolean`, `error` or `formula`), a `status` and, unless the status is `value`, a
stable `reason`:

- `formula` cells are always `not_trusted` (`formula_cached_value_not_trusted`); cached values are
  never read as data.
- `error` cells are `unparsed` (`error_cell`).
- An empty cell is `missing` (`empty_cell`) when `empty_is_missing` is true and `unparsed`
  (`empty_not_missing`) otherwise. An exact `missing_tokens` match is `missing` (`missing_token`).
- A type/kind conflict is `unparsed` with `kind_not_text`, `kind_not_numeric` or `kind_not_boolean`;
  the service never coerces a text cell into a number or vice versa.

Canonical values are deterministic text, never Excel formatting:

- `text` keeps the raw text exactly;
- `integer` requires an integral numeric cell and renders the plain decimal integer;
- `decimal` requires a numeric cell and strips insignificant trailing zeros (`0010.500` → `10.5`);
- `boolean` accepts only the OOXML `1`/`0` cell payload and renders `true`/`false`;
- `date` requires an integral numeric serial and renders ISO `YYYY-MM-DD` using the workbook's
  preflight `date_system`. In the 1900 system serial `1` is 1900-01-01, serial `59` is 1900-02-28,
  serial `60` is the fictitious leap day and stays `unparsed` (`invalid_serial`), and serial `61` is
  1900-03-01. The 1904 system starts at serial `0` = 1904-01-01. Out-of-range serials, fractional
  serials and non-numeric cells are `unparsed` with an explicit reason.

Numeric interpretation is bounded: a numeric token longer than 64 characters is `unparsed`
(`value_too_large`), so `Decimal` arithmetic stays bounded. Currencies, percentages and times of day
are never interpreted, `duplicate_rows` is never evaluated, and the result states
`storage=derived-not-persisted` and `scope=materialized-preview-window`. This is not full-sheet
extraction and not evidence that undeclared or non-previewed cells were inspected.

## Whole-sheet conformance (read-only)

`GET /v1/analysis/files/xlsx/{file_id}/conformance` requires a recorded declaration with at least one
declared column (otherwise `409 xlsx_conformance_no_declared_columns`). It re-runs the same parser and
binding checks as a preview read, then reads every **present** row below the declared header for the
declared columns only. Rows absent from the sheet are never synthesized (`rows=present-rows-only`),
undeclared columns are never materialized, and the response contains no cell text.

Each declared column reports its reading counts (`values`, `missing`, `unparsed`, `not_trusted`), a
sorted breakdown of the non-value reasons, and a per-column digest. The report also carries
`data_rows`, `cells_read`, the exact `interpretation_hash`, the echoed `duplicate_rows` policy,
`duplicate_row_count` (rows whose canonical readings repeat an earlier row over the declared columns)
and a `snapshot_digest` over every row number and reading. Duplicates are counted, never dropped
(`duplicate_rows_handling=counted-not-dropped`), and no snapshot is written.

Readings use the same deterministic rules as the sampled-window resolution: formula caches are never
trusted, error and empty cells keep their stable reasons, and currencies, percentages and times of day
stay uninterpreted. Bounds are explicit: 250,000 declared cells and 16 MiB of declared cell text per
report, beyond which the request fails closed (`xlsx_conformance_cell_limit`,
`xlsx_conformance_char_limit`); the shared processing deadline still applies. The report is derived on
request, is not persisted or audited, and does not change the stored preview, source, confirmation,
declaration or expiry. This is not a `DatasetSnapshot`: it exposes no values and provably reads only
the declared columns of the pinned sheet.

## Number-format facts (read-only)

`GET /v1/analysis/files/xlsx/{file_id}/formats` requires a recorded declaration with at least one
declared column (otherwise `409 xlsx_formats_no_declared_columns`). It re-runs the same parser and
binding checks, resolves the workbook's `cellXfs` style entries through `xl/styles.xml`, and counts,
per declared column over every **present** data row, which number format each cell uses. A workbook
without a style part uses the default General format (`numFmtId=0`); a declared cell the worksheet
omits counts as that default so every declared cell is accounted for exactly once.

Each distinct format is reported as `num_fmt_id`, `kind` (`builtin` for ids below 50, `custom` when the
workbook defines the id in `numFmts`, `undefined` otherwise), the inert custom `formatCode` text, and
the number of declared cells using it. The contract requires the counts to sum to the column's cell
count, forbids duplicate format entries and keeps them sorted. Custom format codes are DLP-scanned
before the response is returned; a denied code produces `404 xlsx_file_unavailable`.

The report states `display_format=inert-not-applied`, `currencies`/`percentages`/`times_of_day` and
`classification` as `not-interpreted`/`not-evaluated`, `values=unchanged`, `dlp=format-codes-scanned`
and `storage=derived-not-persisted`. A format is never applied to a value, never used to reinterpret a
number, and no cell value is returned. Bounds are explicit: 250,000 declared cells, 4,096 `numFmts`
and `cellXfs` entries, and 256 characters per format code, beyond which the request fails closed
(`xlsx_formats_cell_limit`, `xlsx_formats_styles_limit`, `xlsx_formats_code_limit`). Invalid XML,
duplicate or missing `numFmtId` values, malformed style indexes and out-of-range index references fail
closed rather than degrading. Nothing is persisted or audited and the stored record is unchanged.

## Retained derived snapshot and bounded value page

`POST /v1/analysis/files/xlsx/{file_id}/snapshot` requires a recorded declaration with at least one
declared column (otherwise `409 xlsx_snapshot_no_declared_columns`; a file without a declaration
returns `409 xlsx_interpretation_not_declared`). It re-runs the same parser, preview-binding and
declaration-binding checks, reads every **present** row below the declared header for the declared
columns only, and stores the canonical reading of each cell — a status code per row and the canonical
value when the status is `value` — inside the existing encrypted payload. The response contains no
cell text: per-column `cells`/`values`/`missing`/`unparsed`/`not_trusted` counts, `data_rows`, `cells`,
`duplicate_row_count`, the snapshot/interpretation hashes and a `snapshot_hash` over the declaration
hash and every row reading.

The caller owns the retained data: a snapshot is readable only by the same creator/tenant/project/domain
view and expires with the file. `GET .../snapshot` returns the stored metadata without re-deriving
values; a file without a snapshot returns `409 xlsx_snapshot_not_derived`. After a new declaration the
stored snapshot stays readable but reports `status=stale`; deriving again replaces it. Replaying the
same declaration is idempotent: the same `snapshot_hash` is returned without a write or a second audit
event.

Every extracted value is DLP-scanned before persistence — unlike the preview, this covers cells outside
the preview window — and a denial fails the whole derivation with `404 xlsx_file_unavailable`, leaving
no snapshot. Bounds are explicit: 250,000 declared cells and 16 MiB of retained value text, beyond
which the request fails closed (`xlsx_snapshot_cell_limit`, `xlsx_snapshot_char_limit`). The response
states `values=retained-encrypted-not-returned`, `storage=encrypted-derived-not-artifact`,
`dlp=extracted-values-scanned`, `duplicate_rows_handling=counted-not-dropped`,
`dataset_snapshot=false` and `artifact=false`. A successful derivation emits one
`xlsx.snapshot_derived` audit event carrying only hashes and counts. The snapshot is not a
`DatasetSnapshot` and is never registered as an artifact.

`GET /v1/analysis/files/xlsx/{file_id}/snapshot/values` is the only path that returns retained values.
It requires a snapshot derived from the **current** declaration: a stale snapshot returns
`409 xlsx_snapshot_stale` rather than serving old values under a revised declaration, and a file
without a snapshot returns `409 xlsx_snapshot_not_derived`. The page is keyset-based: `limit` is 1–500
(default 100) and `after` is the last retained row number from the previous page; the response echoes
`after`, returns `next` only while `more=true`, and `retained_rows` is the total. Every row carries one
status code per declared column and an aligned `values` tuple where only `value` readings are
non-null; `missing`, `unparsed` and `not_trusted` stay `null` with their code. Only declared columns are
projected, and every response states `values=retained-decrypted-declared-columns-only`,
`dlp=values-rescanned-before-return`, `storage=encrypted-derived-not-artifact`,
`status=current`, `dataset_snapshot=false` and `artifact=false`.

The page's plaintext is DLP-scanned again immediately before it is returned, so a scanner change denies
the page (`404 xlsx_file_unavailable`) even though the values were accepted at derivation. A page over
2 MiB of value text fails closed (`413 xlsx_snapshot_page_char_limit`); the client can reduce `limit`.
A non-empty page emits one `xlsx.snapshot_values_read` audit event (row count and first/last row, never
values) before the response; if audit is unavailable, no page is returned. An empty page — including
one paged past the last row — emits no audit event. This is a declared-column value read, not an
export, not profiling, not an analysis input, and not a `DatasetSnapshot`.

## Read-only retention inventory

`XlsxMaintenanceInventory` is a separate local operator port for reviewing retained and expired
capacity. It opens one existing encrypted intake database with `mode=ro` (never creating schema,
parents or an empty store), verifies the 32-byte key against the stored canary and rejects an
unsupported layout. It never parses a workbook, never returns headers or cell values, never re-derives
or re-scans values, and never writes to the store or audit log.

The v1 `XlsxMaintenancePlan` lists each retained file with file/task IDs, a truncated non-reversible
keyed owner reference, `staged`/`confirmed` status, release compatibility, whether a declaration
exists, and snapshot metadata: `none`/`current`/`stale`, hash, retained row/cell counts and duplicate
row count. It also carries the source hash and byte count, creation/expiry times and a review state
(`retain`, `expired_review_required`). Per creator scope it reports retained/expired file counts and
source bytes; the plan reports the configured 20-per-creator/200-total bounds and remaining headroom.
The response states `values=not-read-by-this-inventory`, `deletes=false`,
`releases_capacity=false` and `audit=not-emitted`.

`inventory_digest` covers immutable retained facts only, so repeating the inventory on an unchanged
store yields the same digest at a later wall-clock time; a new upload, confirmation, declaration or
snapshot changes it. Expiry and `stale` state are observations recomputed on every run. Corrupt
records, a wrong key, an unsupported layout or more rows than the configured bound fail the whole
observation; no partial plan is emitted. The plan is an observation for review, never authorization
to delete, and expired rows still count against capacity until a separately approved cleanup exists.

```bash
uv run python -m generaldataagent.datasets.xlsx_maintenance \
  --store /private/operator/xlsx.db \
  --key-file /private/operator/maintenance.key --summary
```

The key file must be an operator-owned regular file (not a symlink, mode `0600` on POSIX) containing
base64 for exactly 32 bytes. Exit codes: `0` complete plan, `2` key/permission or contract invalid,
`4` store unobservable. Output contains codes, counts, hashes and a truncated scope reference rather
than workbook names, headers or values. This is a local operator CLI, not a browser, model or
tenant-management endpoint. The design record is
[ADR 0033](../adr/0033-read-only-xlsx-retention-inventory.md).

## Approval-gated expiry cleanup

`XlsxSourceCleanup` is a separate local operator port. The operator reviews an
`XlsxMaintenancePlan`, requests one exact R2 approval for its `inventory_digest` and an explicit sorted
list of expired file IDs, then supplies that approval ID together with the existing store and key. A
new `xlsx.maintenance.remove` policy rule allows only `remove-expired-source` for the `data_engineer`
role and requires approval. There is no API route, browser action, model tool or tenant-management
surface for this port.

Execution re-observes the inventory with the same released decoder, refuses any digest drift, a
selected file that is not `expired_review_required`, an unapproved request, or malformed arguments,
and consumes the one-time grant only after those checks. A single SQLite `BEGIN IMMEDIATE`
transaction verifies the exact `user_version`, key tables, key canary and tombstone layout, then
writes one content-free tombstone and deletes the selected `xlsx_uploads` row. Any error rolls back
both; nothing outside the selected rows is touched. The derived snapshot lives inside the deleted row,
so it is removed with the source, while any table the store did not previously have is left absent
after rollback.

`XlsxSourceTombstone` binds file and task IDs, the truncated keyed owner reference, status, snapshot
hash (if any), source hash and bytes, the approval ID and the reviewed inventory digest. It contains no
header text, cell value, formula, idempotency key or key material. `XlsxCleanupReceipt` reports the
requested selection, per-item outcome (`removed`/`already_removed`), reclaimed and remaining source
bytes, and fixed `evidence_artifacts_removed=false`, `safe_erasure=false`,
`destructive_actions_performed=true`. Its digest excludes outcomes and times, so replaying the same
approval after a successful removal yields the same digest. Replay returns `already_removed` and never
deletes twice; if the inventory drifted before the first removal, no tombstone exists and the run is
refused instead.

```bash
uv run python -m generaldataagent.datasets.xlsx_cleanup \
  --store /private/operator/xlsx.db \
  --key-file /private/operator/maintenance.key \
  --approval-db /private/operator/approvals.db \
  --approval-id <uuid>
```

Exit codes: `0` receipt emitted, `2` approval/selection/key invalid, `4` store unobservable, `5`
observed rows or tombstones contradict the approved selection. Free pages, WAL files and backups may
still contain removed bytes; expiration cleanup is not safe erasure and does not release a legal hold
or claim compliance retention. The design record is
[ADR 0034](../adr/0034-approval-gated-xlsx-expiry-cleanup.md).

## Encryption, scope and binding

AES-256-GCM encrypts both the preview payload and the source bytes in a dedicated SQLite database.
AAD binds the file ID, HMAC-derived creator scope, HMAC-derived idempotency key and request hash, so
rows and encrypted bodies cannot be swapped. The database verifies a canary, `user_version` and exact
column layout at open; a wrong key or non-dedicated database is rejected.

The staged record pins the source SHA-256, parser fingerprint (preflight/preview module hashes,
Python version and implementation, text configuration, limits), processing fingerprint, config hash,
preflight digest and preview hash. A read re-parses the decrypted source and requires exact equality
with the stored preflight and preview. Parser or limit drift returns 409 and requires restaging; a
different preview result returns 404 rather than being served.

Retention defaults to 24 hours. Capacity is 20 retained files per creator and 200 overall; expired
rows still count until an operator removes them through the exactly approved cleanup above, and an
expired idempotency key is not reused. The read-only retention inventory observes expired rows and
capacity headroom; cleanup is never automatic and only removes the explicit selection.

## DLP scope

DLP scans the preview text only: sheet name, header, preview rows and formula text. It does **not**
scan the raw ZIP or cells outside the preview window. Every record therefore states
`dlp_scope=preview-text-only`. Credential-like column headers are denied before storage. A declaration
only repeats header labels already present in the scanned preview and is stored inside the encrypted
payload; audit carries the interpretation hash, not the labels or tokens. Three paths touch text not
previously scanned: the number-format report returns custom format codes, so those codes are scanned
immediately before the response and a denial returns 404; snapshot derivation extracts declared values
outside the preview window, so every retained value is scanned before it is persisted and a denial
returns 404; the value page decrypts retained values, so its plaintext is scanned again immediately
before the response and a denial returns 404. Do not treat this preview as evidence that the whole
workbook has been inspected.

## Errors and refusal behavior

| Status | Meaning |
|---|---|
| 413 | byte, processing, conformance-cell/character, format-cell, style-table, format-code, snapshot-cell/character or snapshot-page-character limit exceeded |
| 415 | wrong media type or non-identity content encoding |
| 422 | invalid/macro/structurally rejected workbook, preview rejection, invalid idempotency key or invalid declaration contract |
| 409 | idempotency request changed, retained capacity exhausted, parser/limit drift, changed binding, header row outside the preview, a column mismatch, no recorded declaration, a declaration without columns, no retained snapshot, a stale snapshot or an invalid/expired listing cursor; the response detail carries the stable code |
| 404 | file unavailable to this identity, expired, preview binding failed, declared columns do not bind, or a format code, extracted value or returned snapshot page was denied by DLP |
| 408 | upload disconnected or timed out |
| 503 | intake not configured or the encrypted store is unavailable |

Audit is emitted before insertion. An audit or DLP failure leaves no staged row. Unknown, tampered or
unreadable entries fail closed rather than being partially served.

## Not implemented

- applying declared types to the source bytes, rewriting stored workbooks, dropping duplicate rows or
  producing a persisted whole-sheet conflict summary; the conformance report is derived per request
  and returns counts and digests only;
- exporting retained values, selecting a subset of declared columns, filtering or aggregating on the
  server, or binding a snapshot to an analysis task; the value page returns declared columns only and
  writes no `DatasetSnapshot` or artifact;
- selecting a header row outside the materialized preview window;
- applying number formats, rounding by display format, or deciding that a format means a currency,
  percentage or time of day; the format report exposes inert codes and counts only;
- currency, percentage or time-of-day interpretation and formula-cache trust;
- duplicate-row enforcement, general full-sheet value extraction, profiling or read-only analysis;
- declaration listing, filename/column-name/search listing, total counts, workspace, approvals,
  artifact registration or `DatasetSnapshot`;
- automatic expiry cleanup, scheduled retention enforcement, capacity release without an exact
  approval, legal-hold release or any safe-erasure claim; cleanup is a local operator port that
  removes only the explicitly approved expired rows and leaves a content-free tombstone;
- production identity, production qualification or safe-erasure claims.
