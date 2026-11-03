# Reviewed local display-history maintenance

Inactive display-preference history can be removed in reviewed, bounded SQLite-row batches. The
consumer is an explicit local maintenance CLI over the existing durable preference/session service.
Importing it starts no process. It exposes no agent tool, new HTTP route or production adapter.

## Retention and scope

Each operation uses a current `analysis:run` session, its server-authenticated creator/tenant/project,
and an explicitly requested subset of granted domains. Scope is the exact normalized domain-view.
The owner of one view cannot apply a plan for another view, user, project or tenant.

Only **historical** revisions are candidates. The latest record always survives, including an active
value, expired value or revocation tombstone. Its revision is the high watermark: purging must never
resurrect an older value, reset current state or reuse a deleted revision number.

A historical confirmed revision becomes inactive at the earlier of its expiry or its next retained
successor's creation. A historical tombstone becomes inactive at its successor's creation. Default
citation retention is seven days after inactivity; trusted local operators may select 60 seconds to
90 days. Missing successors after earlier purges can delay eligibility conservatively. Clock inversion
or corrupt scanned/lookahead records denies planning. This is a local retention policy, not production
retention qualification.

After removal, authenticated history reads for that revision return unavailable. Already delivered
presentations still contain their original preference snapshot/hash; server-side historical citations
are not promised forever. Current presentation, certified metric results, checkpoints, budgets and
artifact retention remain unaffected. Expired latest values are retained; DELETE still appends a
revocation, not a physical erase request.

## Dry run and apply

An operator-owned `module:function` returns `MetricDisplayPreferenceService` with a durable
`SQLiteSessionBroker`. Supply the session through `GDA_PREFERENCE_MAINTENANCE_TOKEN`, never argv.
The CLI does not print it or copy it into plans. Keep the factory, keys and state outside agent access.

```bash
# Default operation is a dry run. It does not remove rows.
uv run python -m generaldataagent.services.preference_maintenance \
  --development --factory operator_module:build_preferences \
  --data-domain commerce > reviewed-plan.json

# Review the plan's head, bounded range, candidate revisions and cutoff first.
uv run python -m generaldataagent.services.preference_maintenance \
  --development --factory operator_module:build_preferences \
  --data-domain commerce --apply-plan reviewed-plan.json
```

Plan v1 includes head version, scanned/candidate counts, candidate revisions, cutoff, scan range,
`has_more`, `next_after_version` and an opaque AES-GCM token. It expires after five minutes. The token
binds every reviewed field, candidate record hashes, current head hash, exact private scope and
canonical store location; another location needs a fresh plan even for a cloned DB. The plan is not
a bearer credential: application still requires a current owner session. Candidate data values and
raw credentials are absent from plan/output/audit.

Default scan limit is 50, maximum 200. Planning decrypts at most that many historical records, one
lookahead and the head. `--scan-limit`, `--after-version` and `--retention-seconds` customize a dry run;
they cannot be mixed with `--apply-plan`. Each application affects only the reviewed candidates,
never automatically widens scope or runs another batch. `has_more` refers to unscanned historical
rows, not a complete retention sweep. For the next fresh plan, pass `--after-version` equal to the
previous `next_after_version`; a later full sweep can start at zero.

Apply rechecks the live grant, plan/time/scope/store binding, unchanged head and candidate ciphertext
hashes under one write transaction. Any intervening confirmation/revocation requires a new plan.
Overlapping plans with the same head may find candidates already absent; replay reports separate
`deleted` and `already_absent` counts, not proof that a particular prior invocation committed.

Audit intent precedes deletes. Audit, authorization, expiry, corruption or DB commit failure rolls
back the entire batch; an audit intent can remain on failed commit and is not a success receipt.
The CLI emits fixed errors without exception bodies: exit 2 for unavailable/invalid input, exit 3
for a stale/conflicting plan. It defaults to no mutation and requires explicit `--development`.

## Capacity, compatibility and limits

`max_versions` now bounds **retained rows per exact scope**, instead of lifetime revision number.
Purging old rows releases that capacity; new confirmations still increment the surviving head.
The last revocation is allowed even at capacity. Database and existing record contracts remain v1;
existing readers already support missing exact history versions and never fall back to older values.
Old writers may conservatively reject high revision numbers at their old lifetime cap; they cannot
reuse IDs while the head remains. Rollback disables maintenance and retains the remaining database.

This is logical row removal. It does **not** securely erase SQLite free pages/WAL, backups, audit
metadata, previously delivered snapshots, files or other stores; no disk-space reclamation is claimed.
It provides no legal hold coordination, latest-value redaction, key rotation, whole-DB rollback
protection, automatic production schedule or independent production admission. Do not restore a stale
backup into live service to undo a purge or resurrect old preferences.

```bash
uv run python -m examples.preference_maintenance_demo
uv run pytest tests/test_stage12_preference_lifecycle.py
```

The synthetic demo runs separate CLI processes for plan/apply/replay, verifies history unavailable,
keeps the exact same current metric presentation and appends a new monotonically numbered revision
using released retained capacity. It calls no model or external platform and purges no operator data.
