# Private XLSX staging, pinned preview and declared interpretation

The opt-in local XLSX service accepts workbook **bytes**, applies the bounded structural preflight and
inert text preview, then stores an encrypted preview bound to the exact source, parser, configuration
and limits. An explicit confirmation can pin that preview. A separate declaration can then record the
header row, per-column type, missing-value tokens and duplicate-row policy as **intent only**. It does
not extract every cell, profile values, evaluate formulas, convert dates or currencies, validate a
declaration against cell values, or make model calls. Production API assembly rejects this adapter.

This is the fifth slice of XLSX ingestion. Date/currency interpretation, full-sheet extraction,
listing, analysis and `DatasetSnapshot` are not implemented.

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
| `GET /v1/analysis/files/xlsx/{file_id}` | Current-grant preview of the fixed staged snapshot; the sheet pinned at staging is used |
| `POST /v1/analysis/files/xlsx/{file_id}/confirm` | Strict JSON with `confirmed: true` and the exact source/preview hashes; pins a confirmed preview |
| `POST /v1/analysis/files/xlsx/{file_id}/interpretation` | Strict JSON declaration over a confirmed preview; records intent without rewriting values |

`sheet` is optional at staging and defaults to the workbook's first sheet. An unknown or invalid
sheet is rejected. Later reads ignore a `sheet` query parameter; restage under a new idempotency key
to preview a different sheet.

The request must use the exact media type
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`; generic `application/vnd.ms-excel`
and compressed transfer encodings are rejected.

Same scope/key/source/config/preflight returns the same file and does not extend expiry. Changed input
under the same key returns 409; new input needs a new key/file ID. IDs are server generated. This is
not a task submission endpoint and creates no analysis task.

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
- `type` is limited to `text`, `integer`, `decimal` or `boolean`. `date`, `datetime`, `currency` and
  percentage declarations are rejected until a later slice implements interpretation.
- `empty_is_missing` defaults to `true`; up to 8 literal `missing_tokens` of at most 32 characters
  are recorded. Tokens are neither matched nor normalized.
- `duplicate_rows` is explicitly required: `reject` or `keep-all`. It is recorded, not executed.

The request must use `Content-Type: application/json` and is limited to 65,536 bytes. The resulting
`interpretation` carries `values=declared-only-no-conversion` and a deterministic
`interpretation_hash` over the source/preview/confirmation hashes, header row, column declarations and
duplicate policy. Replaying the identical declaration returns the same interpretation without writing
or auditing again; a different declaration replaces it, emits a new `xlsx.interpretation_intent`
audit event before the encrypted body update, and returns the new hash. An audit failure leaves the
previous interpretation (or none) in place. Listing or selecting these declarations across files is
not implemented.

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
payload; audit carries the interpretation hash, not the labels or tokens. Do not treat this preview as
evidence that the whole workbook has been inspected.

## Errors and refusal behavior

| Status | Meaning |
|---|---|
| 413 | byte or processing limit exceeded |
| 415 | wrong media type or non-identity content encoding |
| 422 | invalid/macro/structurally rejected workbook, preview rejection, invalid idempotency key or invalid declaration contract |
| 409 | idempotency request changed, retained capacity exhausted, parser/limit drift, changed binding, header row outside the preview or a column mismatch; the response detail carries the stable code |
| 404 | file unavailable to this identity, expired, preview binding failed, or declared columns do not bind |
| 408 | upload disconnected or timed out |
| 503 | intake not configured or the encrypted store is unavailable |

Audit is emitted before insertion. An audit or DLP failure leaves no staged row. Unknown, tampered or
unreadable entries fail closed rather than being partially served.

## Not implemented

- applying declared types or matching declarations against cell values; no validation or conflict
  report exists, and a declared type may contradict the preview;
- selecting a header row outside the materialized preview window;
- date, currency or percentage interpretation and formula-cache trust;
- full-sheet extraction, profiling or read-only analysis;
- file listing, declaration listing, workspace, approvals, artifact registration or
  `DatasetSnapshot`;
- production identity, production qualification or safe-erasure claims.
