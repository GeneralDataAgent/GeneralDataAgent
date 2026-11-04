# Read-only retained CSV inventory

The explicit local operator reader inventories the creator's **retained** sources, including expired
ones that the normal file list intentionally hides. It returns bounded metadata and an integrity-bound
comparison plan. It never deletes a source, releases quota, changes an idempotency key, or authorizes a
purge. Default/production API assemblies have no new maintenance route or agent tool.

## Assembly and operator CLI

Use an existing v1 `CsvUploadStore(path, encryption_key=..., read_only=True)`, the existing session
broker and an audit sink to construct `CsvRetentionInspector(store, broker, audit=...)`. Read-only
opening refuses missing/empty/incompatible stores, wrong keys and symlink files; it creates no source
schema/file/parent. Each transaction rechecks schema/key/layout. The connection uses SQLite `mode=ro`,
`query_only`, untrusted schema mode and a 16 MiB row/string limit; write transactions are refused.
This flag concerns the store, not arbitrary writes by other services or a qualified production mode.

```bash
# A fixed operator-owned factory returns a configured inspector with a durable SQLite broker.
# Set GDA_CSV_RETENTION_TOKEN privately in the environment; never put a bearer/key on argv.
uv run python -m generaldataagent.services.csv_retention --development \
  --factory your_operator_module:csv_reader --data-domain commerce > /private/operator/plan.json
uv run python -m generaldataagent.services.csv_retention --development \
  --factory your_operator_module:csv_reader --data-domain commerce \
  --verify-plan /private/operator/plan.json
```

The factory is trusted operator code, not a request/LLM argument. CLI mode is opt-in, derives user,
tenant and project from the current session, and accepts only requested domains covered by that grant.
The inspector rechecks `analysis:run`, identity, domains and revocation before/during the scan and
before output. Constrained business sessions cannot use this port. Audit emission is required; failure
or revocation during audit denies the response. Local JSONL audit remains a development sink.

## What a plan means

A complete exact-owner/domain inventory includes at most 20 retained source entries in file-ID order;
an inconsistent 21st row fails, rather than masquerading as a complete truncated inventory. Each entry
contains IDs, logical raw byte count, creation/expiry, and registered report artifact IDs. Original CSV,
preview/column/sample values, filenames, paths and bearer/session credentials are absent. Counts are
for that exact creator/domain view, not global free capacity or on-disk bytes.

Source ciphertext and its bindings are validated even for expired entries. Registered report metadata
is decrypted and checked against its source/task, expected artifact types and canonical local URI;
artifact contents, index integrity and holds are **not** independently verified. They remain
`registered_not_verified` / `hold_state=unknown`. Active sources are retained; every expired source
is `blocked_hold_unknown`, including staged sources with no registered report. Missing report contents
never becomes deletion clearance. `deletion_authorized=false` and `quota_released=0` are fixed.

The five-minute plan binds scope, physical store reference, observed expiry classification and the
exact encrypted source/report row snapshot. Its HMAC `plan_token` detects edits and cross-scope/path
replay; it is not a bearer token, signed audit, human approval or cleanup capability. Comparing a plan
rechecks current authentication, store bindings, time, rows and expiry classifications. New sources,
new report registration, key/schema/layout changes, expired plans and temporal/store drift fail closed.
The comparison verifies only its read transaction; it is not a lease, CAS or a promise that a writer
cannot change state immediately afterward. A future apply path must independently recheck everything.

The scan has a five-second application guard and SQLite VM checks, with bounded rows/data. These
are not hard timeouts for blocking I/O, locks, crypto, JSON parsing or audit writes. WAL is observed
through a normal read transaction (not `immutable=1`); SQLite may use/create WAL shared-memory files.
No source rows/schema, budgets, artifact index or idempotency state are modified by the reader;
the separately configured audit sink does append metadata. No cross-store atomicity or secure erasure
is claimed. Expired sources still count against retained-file quota.

## Synthetic runnable consumer

```bash
uv run python -m examples.csv_retention_demo
```

The demo stages two real synthetic CSV sources, confirms one through the existing governed profile,
advances its disposable clock past expiry, and invokes the real planning/comparison CLI in separate
processes. It verifies source/budget/artifact-index file hashes are unchanged, expired idempotency is
preserved, and a committed new source invalidates the earlier plan. It uses only temporary synthetic
state and a public disposable key, with zero model/enterprise calls.

Exit codes: 0 successful inventory/comparison, 3 stale/invalid integrity-bound plan, 2 unavailable,
unauthorized or invalid configuration. Error stdout is fixed metadata, never exception diagnostics.
There is no `--apply-plan`, force/hold override, source deletion, tombstone migration, artifact purge,
quota release, browser administration or production qualification in this slice. Rollback removes the
operator command/reader; existing v1 intake data, API contracts and processing fingerprints remain.
