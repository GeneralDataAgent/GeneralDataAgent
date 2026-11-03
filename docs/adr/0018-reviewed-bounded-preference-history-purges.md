# ADR 0018: Bind bounded history-row purges to a reviewed head and live owner grant

Status: Accepted for the local adapter, 2026-11-03.

Confirmed preferences have bounded history but initially consume a lifetime version cap. Operators
need to retire inactive citations without deleting the current revision, reviving an old choice,
resetting CAS numbering or silently widening a reviewed maintenance operation.

Use an explicit local CLI that defaults to a dry-run plan. Encrypt/authenticate its exact reviewed
metadata, candidate hashes, current head, scope and store location; expire it after five minutes.
Each transaction validates the current owner session and unchanged head, then removes only reviewed
inactive historical rows whose citation retention elapsed. Keep the latest value/tombstone as the
high watermark. Read bounded historical windows plus one lookahead; gaps conservatively delay
retention eligibility. Replays distinguish newly deleted and already absent rows.

Charge history capacity to retained row count, not lifetime version number. Existing v1 records and
DB layout already tolerate missing historical versions, so no migration or record rewrite is needed.
Current presentation and original analysis inputs/evidence remain unchanged. Audit intents precede
commit but are not success receipts; all batch failures roll back.

This offers logical row removal only, not secure byte erasure, latest-value redaction, legal holds,
backup rollback detection or production qualification. Disabling maintenance stops new purges;
restoring a stale DB is not a safe rollback. No new API or model write tool is introduced.
