# Private XLSX staging, pinned preview and declared interpretation

The opt-in local XLSX service accepts workbook **bytes**, applies the bounded structural preflight and
inert text preview, then stores an encrypted preview bound to the exact source, parser, configuration
and limits. An explicit confirmation can pin that preview. A separate declaration can then record the
header row, per-column type, missing-value tokens and duplicate-row policy as **intent only**. A
read-only resolution can then derive deterministic per-cell readings for the declared columns over the
same sampled window, and a bounded conformance report can read every present row below the header for
those columns while returning only counts, reasons and digests. A further read-only report counts the
number formats (`cellXfs`/`numFmts`) used by the declared columns without applying them. None of these
calls returns the workbook's undeclared content, extracts values for analysis, profiles data, evaluates
formulas, converts currencies or percentages, rewrites stored values or makes model calls. Production
API assembly rejects this adapter.

This is the ninth slice of XLSX ingestion. The service can now derive whole-sheet conformance and inert
number-format facts for declared columns without returning their values.
Currency/percentage interpretation, full-sheet value extraction, declaration listing, analysis and
`DatasetSnapshot` are not implemented.

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
rows still count until removed by a future cleanup slice, and an expired idempotency key is not
reused. There is no automatic cleanup in this slice.

## DLP scope

DLP scans the preview text only: sheet name, header, preview rows and formula text. It does **not**
scan the raw ZIP or cells outside the preview window. Every record therefore states
`dlp_scope=preview-text-only`. Credential-like column headers are denied before storage. A declaration
only repeats header labels already present in the scanned preview and is stored inside the encrypted
payload; audit carries the interpretation hash, not the labels or tokens. The number-format report is
the one derived response that returns text not previously scanned (custom format codes), so those codes
are scanned immediately before the response and a denial returns 404. Do not treat this preview as
evidence that the whole workbook has been inspected.

## Errors and refusal behavior

| Status | Meaning |
|---|---|
| 413 | byte, processing, conformance-cell/character, format-cell, style-table or format-code limit exceeded |
| 415 | wrong media type or non-identity content encoding |
| 422 | invalid/macro/structurally rejected workbook, preview rejection, invalid idempotency key or invalid declaration contract |
| 409 | idempotency request changed, retained capacity exhausted, parser/limit drift, changed binding, header row outside the preview, a column mismatch, no recorded declaration, a declaration without columns or an invalid/expired listing cursor; the response detail carries the stable code |
| 404 | file unavailable to this identity, expired, preview binding failed, declared columns do not bind, or a returned format code was denied by DLP |
| 408 | upload disconnected or timed out |
| 503 | intake not configured or the encrypted store is unavailable |

Audit is emitted before insertion. An audit or DLP failure leaves no staged row. Unknown, tampered or
unreadable entries fail closed rather than being partially served.

## Not implemented

- applying declared types to stored values, dropping duplicate rows or producing a persisted
  whole-sheet conflict summary or snapshot; the conformance report is derived per request and returns
  counts and digests only;
- selecting a header row outside the materialized preview window;
- applying number formats, rounding by display format, or deciding that a format means a currency,
  percentage or time of day; the format report exposes inert codes and counts only;
- currency, percentage or time-of-day interpretation and formula-cache trust;
- duplicate-row enforcement, full-sheet value extraction, profiling or read-only analysis;
- declaration listing, filename/column-name/search listing, total counts, workspace, approvals,
  artifact registration or `DatasetSnapshot`;
- production identity, production qualification or safe-erasure claims.
