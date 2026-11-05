# ADR 0022: Remove reviewed expired CSV sources under one exact approval

Status: accepted for opt-in local development; production admission unchanged.

## Context

ADR 0021 added a read-only inventory and a stable `inventory_digest` over retained private CSV
sources. The remaining 13.1B.2b question is how an operator can act on that review without letting
an agent, browser or ad-hoc script delete arbitrary rows. Deletion must stay inside the encrypted
intake database, preserve the record-binding checks that protect reports and evidence, remain
auditable and idempotent, and never imply that bytes are unrecoverable from free pages, WAL files or
backups.

## Decision

Treat cleanup as a separate operator port that consumes exactly one existing one-time approval.
The request must use tool `csv.maintenance.remove` and action `remove-expired-source`; its
`resource` must equal the approved plan's `inventory_digest`, and its parameters must contain only
that digest and an explicit sorted, unique list of 1–200 file IDs. The port is invoked locally with
the intake store, key file, artifact index and approval database; there is no browser, model or
tenant-management endpoint.

On execution the port:

1. Loads the exact approval without granting anything and refuses unknown, pending, rejected,
   expired or already-dispositioned requests.
2. Re-observes the inventory with the same read-only checks and requires the observed
   `inventory_digest` to match the approved one. Drift refuses before any grant is consumed.
3. Requires every selected entry to be `expired_review_required`, expired and unblocked. Retained,
   blocked, held or unknown entries refuse the whole selection.
4. Consumes the one-time grant through the approval store's trusted operator path, then opens the
   intake database with `mode=rw` (`BEGIN IMMEDIATE`, symlink/missing paths refused) and verifies
   `user_version`, exact table layouts and the key canary.
5. Inserts one content-free `CsvSourceTombstone` and deletes the `csv_uploads` row for each selected
   file in the same transaction. The tombstone binds file/task IDs, non-reversible owner reference,
   source hash and size, report digest, sorted evidence artifact IDs, approval ID and inventory
   digest, and carries its own digest so tampering is detected on read. It stores no CSV values,
   report text or owner identity.
6. Emits a `CsvCleanupReceipt` with per-file outcomes, reclaimed bytes observed for this run,
   remaining files/bytes observed after the transaction, and fixed facts
   `destructive_actions_performed=true`, `evidence_artifacts_removed=false`, `safe_erasure=false`.

The receipt digest binds the approval, inventory digest, epoch and selected source facts. It
deliberately excludes outcomes, counts and observation times, so replaying the same approval yields
the same digest with `already_removed` outcomes and no further deletion.

Consume-before-delete makes a crash between grant consumption and the store transaction recoverable:
a rerun with the unchanged plan observes the same digest, tolerates the already-consumed grant, and
completes the transaction. If the store has already changed (including a completed removal), a rerun
is accepted only when every selected file has an authenticated tombstone bound to the same approval
and inventory digest and no row remains; that replay reports `already_removed` and never deletes
again. Any other drift, missing row without a matching tombstone, conflicting tombstone or
unreadable tombstone refuses or conflicts without deleting.

## Consequences

Operators can reclaim the selected expired capacity with an auditable, idempotent, approval-bound
action that preserves evidence artifacts, the artifact index and all unselected rows. Policy declares
the tool R2, `data_engineer`-only, approval-required, with audit/tombstone/retention obligations.

Cleanup does not erase pages from the database file, WAL frames or backups, does not release legal
holds, does not delete governed evidence, and does not automatically restore rows. Its capacity
accounting covers only the removed rows. Existing intake, inventory, approval, budget, audit and
production constructor behavior is unchanged; the CLI remains local and file/operator authorized,
and the development pilot does not change production admission.
