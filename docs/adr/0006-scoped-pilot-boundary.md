# ADR 0006: Scoped pilot boundary

Date: 2026-10-18. Status: accepted for local pilot implementation.

## Decision

Keep the existing local API compatible. Production factory configuration must include an
explicit session broker. The broker receives trusted identity-provider grants; API callers
cannot issue sessions or choose grants. Use opaque random tokens, store only their digests,
limit lifetime to 900 seconds, and verify user/tenant/project/domain on every versioned API
request before invoking a runtime. Deny access to unscoped review storage.

Expose a dynamic service-stop callback that fails closed on errors. Keep health available.
Evaluate release measurements against the planned thresholds and the deployed revision;
do not mistake unsigned measurements for independently verified deployment authorization.

Minimize local audit payloads using a fixed allowlist. Full DLP and independently controlled
audit storage remain separate production requirements.

## Consequences

No new cryptographic token format or permissive JWT verification is introduced. The implemented
OIDC adapter and shared revocation store require enrollment with the real provider and deployment
under protected service identities. RLS/CLS must
be enforced by source connectors under workload identity, rather than inferred from a valid
API session. The default development app is not a production entry point. Release evidence
must be independently authenticated and SLOs observed before controlled-user admission.

## Rollback

Disable versioned traffic using the service switch. Explicitly revoke sessions in the shared
broker; restarting a service does not invalidate durable sessions. Revert the feature commits only in a development
environment; never restore header-only identity as a production fallback.
