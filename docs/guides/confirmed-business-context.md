# Confirmed business definitions and project background

The local opt-in context memory stores bounded background the person explicitly chose to remember:
up to 50 `business_definition` or `project_context` entries per confirmed snapshot. It is **cited
context only**. It cannot grant permissions, change tool policy, bypass approval or budgets, alter a
certified metric calculation, or rewrite an accepted task payload. It is not a general agent write
tool, and execution-time injection remains a separate governed slice.

## API and assembly

Construct `ConfirmedBusinessContextStore` with a dedicated database, an operator-owned 32-byte AES
key, a DLP scanner, an audit sink, and optionally a callable returning the certified metric terms of
the active catalog. Keys and databases stay outside agent/artifact workspaces.
`BusinessContextService` must share the API's existing `MetricTaskService` and live broker. Pass it as
`create_app(..., business_context=service)`. The routes are disabled by default and production
assembly explicitly rejects this local adapter.

Existing bearer/scope headers and `analysis:run` apply; no actor/source identity is accepted in JSON.
Private scope is the exact normalized creator/tenant/project/domain-view. Another user, project,
tenant, or requested domain-view cannot retrieve the record.

| Method and path | Contract |
|---|---|
| `GET /v1/analysis/context` | Version/state; active record only |
| `PUT /v1/analysis/context` | Confirm snapshot with expected_version CAS and strict `confirmed: true` |
| `DELETE /v1/analysis/context?expected_version=N` | CAS revocation of active retrieval; history retained |
| `GET /v1/analysis/context/versions/N` | Authenticated historical citation; never active retrieval |

```json
{
  "schema_version": "1.0",
  "expected_version": 0,
  "confirmed": true,
  "value": {
    "schema_version": "1.0",
    "entries": [
      {"kind": "business_definition", "term": "GMV", "text": "含税成交总额"},
      {"kind": "project_context", "term": "Q4", "text": "本季度只看自营渠道"}
    ]
  },
  "valid_for_seconds": 2592000
}
```

Terms are 1–100 single-line characters, text is 1–2000 characters, and at least one entry is
required: clearing memory is an explicit `DELETE`, never an empty confirmation. Entries are stored in
canonical `(kind, term)` order and duplicate normalized terms — case/width-insensitive, NFKC — are
refused as an ambiguity that requires clarification rather than a silent overwrite. A
`business_definition` may not redefine a certified metric term; the definition lives in the catalog,
and the API returns `409 business_context_cannot_override_certified_metric`. `project_context` may
mention a certified term.

Expected version 0 means unset. Parallel changes to one revision have at most one winner; stale CAS
returns 409. A lost PUT response may already have committed: read current state before retrying. The
source is a server-issued confirmation/revocation ID bound to the authenticated actor, creation time,
validity, revision, content hash and whole-record hash. It is evidence of what the user chose to
remember, not certification of external business knowledge. Validity is 1 second to 365 days
(default 30 days). Records are encrypted with scope/revision AAD; a key sentinel rejects accidental
wrong-key reopens. Unknown schema, corruption, missing audit or DLP rejection never becomes active
memory.

## State and recovery

State is `unset`, `active`, `expired` or `revoked`; only `active` carries a record. An expired or
revoked latest revision never falls back to an older confirmed value — callers receive an explicit
inactive state, and are expected to ask the user again. History is readable by exact scope for
citation only and must not be treated as current context. Reopening state and issuing a new session
preserves the same revision. Updating context affects a later read, while an old citation carries its
original immutable revision and hashes.

## Audit and limits

DLP checks typed content before persistence. Change-intent audit is required before commit, and the
live grant is checked before/after the insert. Failed audit, CAS, DLP, authorization or DB commit
leaves no new active revision. An audit intent can remain if a later commit fails; it does not prove
state changed, and it carries only revision plus record hash — never the remembered text.

Revocation is **not physical erasure**: encrypted history remains available for private citations
until a separately reviewed retention policy removes an inactive row. Default capacity is 1000
retained rows per exact scope (operator configurable 1–10000); further confirmations fail closed, but
revoking the latest revision and citing history are still allowed. Secure erasure, key rotation,
shared/production storage, artifact-sourced qualification of definitions, and execution-time context
injection remain later lifecycle work. Rollback disables context assembly and retains the encrypted
DB/key/audit; analysis, result and display-preference endpoints keep their existing contracts.

```bash
uv run python -m examples.business_context_demo
uv run pytest tests/test_stage12_business_context.py
```

This synthetic ASGI demo refuses redefining a certified metric, confirms two entries, reopens state
with a new session, revokes it exactly and verifies the historical citation. It makes zero
model/external calls and never starts a task. This is development evidence, not measured business
benefit, independent acceptance or production admission.
