# Confirmed private display preferences

The local opt-in preference service stores only `MetricDisplayPreference` v1: a `summary`/`table`
layout and 1–50 preview rows. An authenticated user explicitly confirms each change. This is the
first private memory consumer, **completed metric result presentation**; it does not store business
facts, metric definitions, model-extracted candidates, arbitrary instructions or credentials.

## API and assembly

Construct `ConfirmedDisplayPreferenceStore` with a dedicated database, an operator-owned 32-byte
AES key, a DLP scanner and an audit sink. Keys and databases stay outside agent/artifact workspaces.
`MetricDisplayPreferenceService` must share the API's existing `MetricTaskService` and live broker.
Pass it as `create_app(..., display_preferences=service)`. Default routes are disabled and production
assembly explicitly rejects this local adapter.

Existing bearer/scope headers and `analysis:run` apply; no actor/source identity is accepted in JSON.
Private scope is the exact normalized creator/tenant/project/domain-view. Another user, project,
tenant, or requested domain-view cannot retrieve the record. Broader caller headers never substitute
another domain's preference for a task's original domain-view.

| Method and path | Contract |
|---|---|
| `GET /v1/analysis/preferences/display` | Version/state; active record only |
| `PUT /v1/analysis/preferences/display` | Confirm body with expected_version CAS and strict `confirmed: true` |
| `DELETE /v1/analysis/preferences/display?expected_version=N` | CAS revocation of active retrieval; history retained |
| `GET /v1/analysis/preferences/display/versions/N` | Authenticated historical citation; never active retrieval |
| `GET /v1/analysis/tasks/{task_id}/presentation` | Evidence-rechecked completed result plus a bound display snapshot |

```json
{
  "schema_version": "1.0",
  "expected_version": 0,
  "confirmed": true,
  "value": {"schema_version": "1.0", "layout": "summary", "preview_rows": 10},
  "valid_for_seconds": 86400
}
```

Expected version 0 means unset. Parallel changes to one revision have at most one winner; stale
CAS returns 409. A lost PUT response may already have committed: read current state before retrying.
The source is a server-issued confirmation/revocation ID and operation kind, bound to the authenticated
actor, creation time, validity, revision, content hash and whole-record hash. It is evidence of the
user's display choice, not certification of external business knowledge. Validity is 1 second to
30 days. Records are encrypted with scope/revision AAD; a key sentinel rejects accidental wrong-key
reopens. Unknown schema, corruption, missing audit or DLP rejection never becomes active memory.

## Presentation and recovery

The presentation endpoint reuses the completed-result evidence reader: current creator/grant,
checkpoint request, validation, artifact hash/size/content and delivery bindings are rechecked.
It neither resumes a graph nor queries a data source, calls a model, resets budget, writes artifacts,
changes certified metric calculation, or rewrites original accepted queue inputs.

`MetricTaskPresentation` v1 includes the unchanged result hash, original task/thread IDs, complete
private preference revision, bounded preview, original row count/summary/evidence, `has_more_rows`
and a presentation hash. Summary layout intentionally has no preview rows; full data remains behind
the authenticated existing `/result` endpoint. Preference defaults are table/20 rows. Missing,
revoked or expired preferences explicitly use these defaults, with their state exposed. Corrupt
memory does not silently default. A preference change/expiry/revocation during the read returns 409;
revoked grant or invalid result evidence denies the response.

Reopening state and issuing a new session preserves the same revision and presentation. Updating a
preference affects a subsequent presentation, while an old snapshot carries its original immutable
revision and hashes, back-readable through the history endpoint. Historical citations may be expired,
revoked or superseded; callers must not treat them as current context. Analysis inputs and checkpoint
recovery are unaffected because this slice consumes memory **after** analysis. Execution-time business
context injection still needs its own versioned acceptance/snapshot protocol.

## Audit and limits

DLP checks typed content before persistence. Change-intent audit is required before commit, and the
live grant is checked before/after the insert. Failed audit, CAS, DLP, authorization or DB commit leaves
no new active revision. An audit intent can remain if a later commit fails; it does not prove state
changed. Presentation-intent audit can likewise remain on a denied read. No cross-store atomicity,
external signed audit or full-database rollback detection is claimed.

Revocation is **not physical erasure**: encrypted history remains available for private citations.
Default history capacity is 1000 revisions per exact scope (operator configurable 1–10000); further
confirmations fail closed, but revoking the last choice is still allowed. Automated purge, erasure,
key rotation, business-definition conflicts and shared/production storage remain later lifecycle work.
Do not expose this store as a general agent write tool. Rollback disables preference assembly and
retains the encrypted DB/key/audit; original analysis/result endpoints continue using their contracts.

```bash
uv run python -m examples.display_preferences_demo
uv run pytest tests/test_stage12_display_preferences.py
```

This synthetic ASGI demo confirms a choice, completes one metric task, reopens state with a new
session, presents the same revision, revokes it and verifies default presentation with the same result
hash. It makes zero model/external calls. This is development evidence, not measured business benefit,
independent acceptance or production admission.
