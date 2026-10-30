# ADR 0011: Persist project and creator artifact read policies

Status: Implemented after the user's explicit 2026-10-30 choice to support both policies.

## Decision

New governed artifact records carry `project` or `creator` access and the bound actor's user ID.
The store constructor selects a default for new writes, while reads and lifecycle operations use
the stored policy. Project sharing retains tenant, project, data-domain and exact-reference checks;
creator access adds exact user equality. Producer-role `created_by` is not an ownership identity.

The runtime/store boundary now also binds the caller's user ID. Otherwise a caller could borrow a
different principal's project-configured store to read its private records. Sharing uses separate
principal-bound readers over the same index rather than relaxing this boundary.

Use a distinct v2 table for all new records. An older binary cannot safely interpret private access,
so it must not find new records in its legacy lookup. The current reader supports both layouts in
one lookup snapshot and rejects duplicate identities. Legacy rows keep project-sharing semantics
and unknown owners without re-encoding or inventing attribution. Index creation is transactional;
unknown partial layouts are refused. Older binaries cannot serve newly written shared records
either; only legacy records remain visible across rollback.

The same persisted rule covers read/export, compensation, holds and expiry cleanup. Existing
retention, DLP, content integrity, task references and stricter application checks remain in force.
There is no automatic policy conversion, owner backfill or administrator bypass. The index remains
trusted protected storage; these records do not authenticate a user or prove production readiness.

See [artifact access](../guides/artifact-access.md) for configuration and compatibility behavior.
