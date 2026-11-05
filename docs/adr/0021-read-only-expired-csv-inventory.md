# ADR 0021: Inventory expired private CSV sources read-only before cleanup

Status: accepted for opt-in local development; production admission unchanged.

## Context

Private CSV staging hides expired files from preview, confirmation, profiling and metadata
discovery, but their encrypted rows and governed evidence remain on disk and continue to count
toward quota. An operator has no honest way to review retained/expired capacity, stable source or
report references, or whether a cleanup candidate is held. Deleting from an ad-hoc script would
bypass record decryption checks, report rebinding, legal holds and evidence scope checks.

## Decision

Add an independent operator port that opens one explicitly configured intake database and the
governed artifact index with SQLite `mode=ro`. It refuses to create missing paths, schema or
parents, verifies the stored key canary, `user_version` and exact table layouts, and decodes every
retained record with the released record-binding checks. It never parses CSV, executes profiling,
writes audit or changes budgets.

Emit a bounded v1 `CsvMaintenancePlan` with expiry observation, retained/expired counts and bytes,
source hashes and sizes, file/task IDs, non-reversible owner references, report state/ciphertext
digest, sorted evidence artifact IDs and a review state. For an expired profiled file, check each
evidence reference read-only against the artifact index; missing, deleted, expired, scope-mismatched
or held evidence blocks the file. A report that cannot be decrypted and rebound blocks it too.
Corrupt records, wrong keys, unsupported layout or an unobservable artifact index reject the whole
observation rather than producing a partial permit.

Bind immutable retained facts into `inventory_digest`; exclude wall-clock expiry and current hold
state so repeated reads are stable while a new upload, confirmation or changed report is detectable.
Always report `destructive_actions_performed=false`. The plan is a review snapshot, never a delete
authorization.

## Consequences

An operator can review expired capacity and blocked evidence without mutating state. Every blocked
file makes the plan `blocked`; a complete plan is still only an input to the separate 13.1B.2b
reviewed maintenance slice. Physical/logical cleanup, tombstones, idempotency, hold release,
crash recovery, capacity accounting and safe-erasure claims remain unimplemented. The CLI is local
and file/operator authorized, not a browser, model or tenant-management endpoint. Default-off
assembly, existing intake contracts and production constructor rejection remain unchanged.
