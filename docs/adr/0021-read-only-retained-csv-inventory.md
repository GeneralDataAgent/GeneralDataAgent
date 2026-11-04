# 0021: Read-only retained CSV inventory before source maintenance

Status: accepted for local development only.

Expired private CSV sources are hidden but remain encrypted in the dedicated store and count toward
capacity. A maintenance consumer must observe this retained state before any future cleanup decision.

Use an explicitly read-only existing-store port and current creator/domain authentication, with
required audit and bounded metadata. Return a five-minute integrity-bound snapshot for review and
read-only comparison. Validate encrypted source/report bindings, but mark report content/holds as
unknown and block all expired rows from deletion. Preserve v1 schema, original processing fingerprints,
source/idempotency rows, artifacts and budgets. Do not add a browser/agent administration endpoint.

This is inventory, not approval or cleanup. Future source retirement needs independently checked
hold/reference eligibility, tombstones preserving original keys, recovery/quotas and separately tested
apply semantics. A successful snapshot comparison is neither cross-store atomicity nor a production
retention/secure-erasure qualification. See the [operator guide](../guides/csv-retention-inventory.md).
