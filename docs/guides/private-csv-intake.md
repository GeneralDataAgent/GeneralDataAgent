# Private CSV text intake and confirmed profiling

The opt-in local CSV service accepts file **bytes**, returns an opaque private file ID and preview,
and profiles only after explicit confirmation of the accepted source and preview hashes. It uses
`DataEngineeringRuntime` → `ToolGateway` → read-only `SQLiteConnector` → existing `data.profile`,
then stores creator-private governed profile/provenance artifacts. It does not register a certified
metric, execute caller SQL or make model calls. Production API assembly rejects this adapter.

## Assembly and API

Construct `CsvUploadStore` with a dedicated database and operator-provided 32-byte AES key, plus the
existing session broker, `TaskBudgetLedger`, DLP scanner and audit sink. Construct `CsvIntakeService`
with separate trusted scratch, artifact content and artifact-index paths. Keep state/key/scratch
outside agent-accessible content. Pass `create_app(session_broker=broker, csv_intake=service)`;
identity brokers must match. Default routes return 503.

Existing bearer/identity/domain headers and `analysis:run` apply. File scope is creator/tenant/project
and the exact normalized domain-view; broader or narrower headers select another view, not a share.
No actor, host path, file name, connector, SQL or artifact-root configuration is accepted in JSON.

| Endpoint | Behavior |
|---|---|
| `POST /v1/analysis/files/csv` | Raw `text/csv` (optional `charset=utf-8`), `Idempotency-Key`; 201 private staged preview |
| `GET /v1/analysis/files/csv?limit=10&cursor=...` | Creator-private bounded metadata discovery; no source values |
| `GET /v1/analysis/files/csv/{file_id}` | Current-grant preview of the fixed accepted snapshot |
| `POST /v1/analysis/files/csv/{file_id}/confirm` | Bounded JSON with strict confirmation and matching source/preview hashes; verified profile |
| `GET /v1/analysis/files/csv/{file_id}/profile` | Verified stored result, without re-executing SQL |

```json
{
  "schema_version": "1.0",
  "confirmed": true,
  "source_hash": "sha256:<64 lowercase hex digits from preview>",
  "preview_hash": "sha256:<64 lowercase hex digits from preview>"
}
```

Same exact scope/key/source/config/release returns the same file and does not extend expiry or reset
budget. Changed input under the same key returns 409; new input needs a new key/file ID. File/task IDs
are server generated. `CsvFileRecord`, preview, confirmation, provenance and profile result are separate
v1 contracts; these are not Stage 11 asynchronous metric jobs or SSE lifecycle events.

## Discovery after refresh

The separate v1 `CsvFileSummary` / `CsvFilePage` contracts expose only file/task IDs, staged/profiled
registration status, release compatibility and creation/expiry times. They exclude names, paths,
headers, samples, row values and evidence bodies. Reopen the same store with a fresh current session,
list files, then fetch the selected preview and explicitly confirm its hashes. Discovery never
re-uploads, re-parses, re-profiles, resets execution budgets or refreshes source retention.

Pages default to 10 items (1–20). Membership and ascending `(created_at, file_id)` order are fixed on
the first page; later uploads appear only in a fresh listing. Registered status is read afresh per
page, and expired/missing items are omitted, so pages can be short or empty. An encrypted opaque
cursor retains only remaining IDs and a fixed five-minute lifetime. It binds the exact creator/
tenant/project/domain view, page size and parser/processing release, but permits a fresh session for
the same current identity. Invalid/tampered/expired cursors or changed page size/release return 409;
restart without a cursor. Continuation does not extend its expiry. There is no cross-page immutable
status snapshot, global total, implicit project sharing or page-number offset.

`availability=compatible` means matching parser/limits/processing fingerprints only. A release-
incompatible record stays discoverable with `release_incompatible`; preview/confirmation/result
still refuse reinterpretation. `status=profiled` means a report was registered, **not** that listing
has verified its ciphertext/artifacts or that evidence remains deliverable. Only the profile endpoint
checks and delivers that evidence; failure never triggers automatic re-profiling. UI must present
these as separate facts and handle 404/409 on subsequent reads.

Every page rechecks live grants and time limits during the bounded inventory scan and before
delivery; expiry is filtered again after audit. Scoped inventory corruption fails the whole read,
never a partial success, and other scopes are not decrypted. At most 20 retained rows are decoded
for one scope (a 21st row refuses the read). Metadata access emits `csv.files_listed` with count and
continuation presence only; audit failure denies delivery. This remains a bounded local adapter,
without a production disk-I/O/ingress SLA. Listing does not perform content DLP scans because it
returns no uploaded text; preview/result access retains the existing scans.

## Explicit text semantics

Only strict UTF-8 with an optional initial BOM, comma delimiter, double-quoted fields/doubled quote
escapes and a first-row header are supported. CR/LF/CRLF record endings and quoted multiline cells
are preserved. Blank logical records, header-only files, ragged rows, malformed quotes, NUL/unsupported
controls and empty/whitespace/ambiguous duplicate headers fail; nothing is silently dropped/renamed.
Unicode compatibility normalization is used only to detect duplicate labels, never to change values.

Every value stays TEXT, including leading zeroes, dates, currency strings and empty strings. Empty
strings remain non-NULL; preview reports per-column empty counts and duplicate rows separately.
`DataProfile` min/max are lexical for TEXT, mean is null, and SQL null counts do not imply empty cells
are complete business values. Formula-like prefixes are counted heuristically, including negatives;
they remain inert text and are never executed. This profile is not numeric/currency analysis or a
quality certification. Type confirmation/conversion, XLSX, formulas/caches and CSV exports are later
slices; no formula-safe spreadsheet export is claimed here.

The parser uses the stdlib's text reader without numeric conversion and preserves newline handling
as described in the [Python CSV documentation](https://docs.python.org/3.12/library/csv.html). It adds
explicit quote/shape/allocation bounds rather than changing the process-global CSV field-size limit.

## Bounds, provenance and recovery

Defaults (operators may only tighten): 1 MiB raw bytes, 1000 data rows, 32 columns, 4096 UTF-8 bytes
per cell, five preview rows and ten seconds per request. Raw-field allocation is checked before the
stdlib parser. Actual streamed byte count is enforced even with a false Content-Length; compression
and workbook/archive inputs are unsupported. Confirmation JSON is capped at 1024 bytes. Streaming
uses an async timeout; parse/import/profile have cooperative checks, an execution ledger and a SQL
cancellation timer. This is not a hard bound for arbitrary blocking disk/audit I/O or a production
HTTP ingress/SLO qualification.

AES-GCM staging binds normalized actor, source bytes/hash, config, parser/runtime/release fingerprints,
preview, IDs and expiry. Fingerprints are captured for the loaded process release, not later mutable
on-disk source. Changed Python/parser/limits or processing release refuses confirmation/read rather
than silently reinterpret an accepted source; explicitly re-stage under a new key. Reopening the same
release with a fresh current session preserves accepted input and confirmed result.

Confirmation materializes only the confirmed text into a private temporary SQLite table using quoted
identifiers and bound values. Existing policy/audit/budget controls authorize profiling; the SQLite
connector now explicitly closes each connection before scratch removal (connection context management
alone does not close it; see [Python SQLite documentation](https://docs.python.org/3.12/library/sqlite3.html#how-to-use-the-connection-context-manager)).
Concurrent confirmations serialize; a committed report is reused without another profile call. A
failed uncommitted read-only profile may run again within its **original** execution budget. Budget
registration is idempotent and never resets spent calls/deadline. Committed result reads have a new
bounded read request, not a new execution allowance.

Each result includes immutable data-profile and CSV provenance ArtifactRefs. Current grant/file TTL,
source/config/preview bindings, report ciphertext, artifact scope/hash/size/content and text-profile
shape are rechecked before first delivery and later reads. Artifact URIs are not public download URLs.

## Read-only maintenance inventory

`CsvMaintenanceInventory` is a separate operator port for reviewing retained/expired capacity. It
opens one existing encrypted intake database with `mode=ro` (never creating schema, parents or an
empty store), verifies the 32-byte key against the stored canary and rejects an unsupported layout.
It never parses CSV, re-profiles, reads preview values or changes the store, artifact index or audit.

The v1 `CsvMaintenancePlan` lists each retained file with file/task IDs, a non-reversible owner
reference, staged/profiled status, release compatibility, source hash/size, creation/expiry,
report state and digest, sorted evidence artifact IDs and a review state
(`retain`, `expired_review_required`, `blocked`). `destructive_actions_performed` is always `false`.
No raw rows, preview samples, headers, paths, artifact URIs or bearer credentials appear.

For an expired file with a report, every evidence reference is checked read-only against the
governed artifact index: missing, deleted, expired, scope-mismatched or held evidence blocks the
file instead of producing an apparently complete cleanup permit. A report that cannot be decrypted
or rebound is `report_unreadable` and also blocks. Corrupt retained records, wrong keys, an
unreadable artifact index needed for verification, or more rows than the configured bound fail the
whole observation; no partial plan is emitted. Any blocked file makes the plan `blocked`.

`inventory_digest` covers immutable retained facts only, so repeating the inventory on an unchanged
store yields the same digest at a later wall-clock time; a new upload, confirmation or changed
report changes it. Expiry and hold state are observations, not part of that digest, and are always
recomputed. The plan is a snapshot for review, never authorization to delete: physical/logical
cleanup, tombstones, capacity release and crash recovery remain the separate 13.1B.2b slice.

```bash
uv run python -m generaldataagent.datasets.csv_maintenance \
  --store /private/operator/uploads.db \
  --key-file /private/operator/maintenance.key \
  --artifact-index /private/operator/artifact-index.db --summary
```

The key file must be an operator-owned regular file (not a symlink, mode `0600` on POSIX) containing
base64 for exactly 32 bytes. Exit codes: `0` complete plan, `3` blocked plan (still printed for
review), `2` key/permission invalid, `4` store/index unobservable. Output contains codes, counts and
digests rather than customer paths. This is a local operator CLI, not a browser or model tool. The
design record is [ADR 0021](../adr/0021-read-only-expired-csv-inventory.md).

## Privacy, failures and limits

DLP checks raw and semantic row JSON before staging and again on reads; common credential columns
are rejected explicitly. Existing pattern checks are not a general anonymizer. Governed result writes
and exports receive DLP again and use CREATOR policy, never implicit project sharing. Audit intents
contain IDs/hashes/control metadata, not raw rows or bearer credentials.

Unknown files/scopes return 404 after authentication; mismatched confirmation, changed release,
missing stored result or exhausted budget returns 409. Unsupported media/compression returns 415;
input/processing limits return 413; malformed input 422; incomplete/timed-out uploads 408; unavailable
storage/audit returns a fixed 503. Underlying exception bodies/paths/rows are not exposed.

Truncated/denied uploads never register partial state. Profile failures roll back report registration,
close/delete scratch and attempt to discard all newly created artifacts; verified evidence is never
repaired by automatically re-running SQL. Staging, execution ledger, artifact index and audit are
separate stores: an audit intent, spent budget or private orphan may survive failed/crashed work;
cleanup can be blocked by storage failure/legal hold. No cross-store atomicity or exactly-once SQL is
claimed. Orphan reconciliation and production maintenance remain later work.

Default file TTL is 24 hours (only tighten); expiry denies all intake reads/confirms but does not
physically erase encrypted source. Quotas include retained expired rows: at most 20 per exact actor
view and 200 globally. The read-only inventory above reports retained/expired capacity and blocks
unverifiable evidence; retention cleanup, sharing, raw download, safe erasure and production
capacity are not implemented by this slice. Governed artifacts retain their own existing retention/
hold rules; no global hold coordination or production data acceptance is implied. Rollback disables
CSV assembly and retains encrypted state/keys and evidence; old metric endpoints remain unchanged.

```bash
uv run python -m examples.csv_intake_demo
uv run python examples/csv_maintenance_demo.py
uv run pytest tests/test_stage13_csv_intake.py
uv run pytest tests/test_stage13_csv_listing.py
uv run pytest tests/test_stage13_csv_maintenance.py
```

The synthetic ASGI intake demo uploads an actual client-selected CSV byte stream, reopens durable
state, discovers the private file through metadata listing, confirms text semantics, profiles
through existing tools, and re-reads the same report/evidence with clean scratch. The maintenance
demo profiles and stages actual bytes, applies a legal hold, advances a fake clock past retention,
then reads the plan twice and asserts stable digest, unchanged store/index bytes, blocked held
evidence and drift after a new upload. Neither demo deletes source or evidence, releases capacity,
or uses a real enterprise data source, model or held-out benchmark question.
