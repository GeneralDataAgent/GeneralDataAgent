# Stage 4 production readiness

## Increment 1: scoped short-lived identity

`create_app(session_broker=..., production_mode=True)` requires a trusted session broker.
An external SSO adapter authenticates the user and resolves authorized project/domain grants
before calling `SessionBroker.issue`; there is deliberately no public token-issuance endpoint.
Sessions expire within 15 minutes, support revocation, and store only token digests.
Every `/v1/` request must match the session's user, tenant, project and authorized domains.
Health remains public. Review endpoints fail closed until their storage has tenant ownership.
The default app remains a local development surface; production must use the secured factory.

Validation: expiry boundary, revocation, forged user/tenant/project/domain, missing credentials,
closed review access and missing production broker. Local sessions invalidate on restart and
are not suitable for multi-worker deployment; external SSO, shared session storage and native
source RLS/CLS remain required before production acceptance.

## Remaining slices

- Service kill switch and fail-closed release gate.
- DLP and retention/deletion enforcement across artifacts, logs and traces.
- Independently authorized audit service and native database RLS/CLS propagation.
- SSO integration, scoped HITL recovery and security/fault-injection exercises.
- At least 90 versioned regression tasks, SLO measurement, alerts and recovery runbooks.

Stage acceptance remains incomplete until independent deployment evidence exists.
