# Private XLSX staging and pinned preview

The opt-in local XLSX service accepts workbook **bytes**, applies the bounded structural preflight and
inert text preview, then stores an encrypted preview bound to the exact source, parser, configuration
and limits. An explicit confirmation can pin that preview without interpreting columns or values. It
does not extract every cell, profile values, evaluate formulas, convert dates or currencies, or make
model calls. Production API assembly rejects this adapter.

This is the fourth slice of XLSX ingestion. Header/type interpretation, full-sheet extraction, listing,
analysis and `DatasetSnapshot` are not implemented.

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
- the first present row is treated as the header;
- empty/duplicate headers, row/column truncation and formulas inside the preview window are reported.

Preview limits are 5 data rows and 64 columns, at most 32,768 characters per cell and 65,536
characters in total. All limits reject rather than truncate a cell or return partial content.

## Explicit confirmation

Confirmation changes only the staged record status and pins a confirmation digest; it does not select
a header row, infer a column type, convert dates/currencies, profile values or run analysis.

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
`dlp_scope=preview-text-only`. Credential-like column headers are denied before storage. Do not treat
this preview as evidence that the whole workbook has been inspected.

## Errors and refusal behavior

| Status | Meaning |
|---|---|
| 413 | byte or processing limit exceeded |
| 415 | wrong media type or non-identity content encoding |
| 422 | invalid/macro/structurally rejected workbook, preview rejection or invalid idempotency key |
| 409 | idempotency request changed or retained capacity exhausted |
| 404 | file unavailable to this identity, expired, or preview binding failed |
| 408 | upload disconnected or timed out |
| 503 | intake not configured or the encrypted store is unavailable |

Audit is emitted before insertion. An audit or DLP failure leaves no staged row. Unknown, tampered or
unreadable entries fail closed rather than being partially served.

## Not implemented

- header-row selection or column type/missing/duplicate confirmation;
- date, currency or percentage interpretation and formula-cache trust;
- full-sheet extraction, profiling or read-only analysis;
- file listing, workspace, approvals, artifact registration or `DatasetSnapshot`;
- production identity, production qualification or safe-erasure claims.
