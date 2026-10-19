# ADR 0008: Qualified service assembly is separate from the local pilot

Status: accepted for implementation; real deployment/acceptance remains blocked.

The original Stage 5 local outbox remains useful for engineering and review. It cannot establish
native receiver delivery, human authentication or independent audit. Removing its production guard
would erase that boundary. The implemented production ports use separate qualified types instead.

`QualifiedBusinessActionRuntime` requires protected fresh operator state, shared revocable sessions,
a requester-only HTTPS approval client, a verified independent HTTPS auditor, a live signed release
and individually signed qualifications for enabled destinations. The qualification hash binds the
actual receiver/control/approval/audit configuration. Signed transition acknowledgements bind the
complete request fingerprint and chain; resource heads and action states must agree with that
history. Local pilot databases cannot be promoted. Per-destination proof expiry/revocation blocks
new writes even when an earlier proposal was approved.

The separate approval authority authenticates human reviewers and trusts only their current
operator enrollment in the same scope. A requester cannot self-review; confirmation is bound to
exact content. Production authority initialization needs private fresh state and a signed audit
profile. Business action IDs acquire one sealed approval request in this independent store, so
restoring business state cannot mint a second grant for an already consumed action.

`QualifiedFeedbackRegistry` adds current authenticated reviewer/curator enrollment, process/thread
exclusion and independently signed state snapshots around mutations. Two independent accepted
votes, immutable datasets and task/result holdout separation remain mandatory. Unacknowledged
native changes quarantine the registry instead of automatically replaying or certifying state.

The profile targets a single POSIX host with operator state outside agent mounts. Filesystem
ownership, service isolation, native identity/receiver grants, retention/legal holds and coordinated
backup/restore are operational requirements. Audit keys are independent of application identities;
retained historical public keys are needed to verify retained acknowledgements. Complete historical
snapshot restores must be reconciled externally before a profile is re-enabled; signed local history
alone is not a remote monotonic-state oracle.

Tests use clearly synthetic identities, ephemeral keys, fake HTTPS transports and fixture evidence.
They verify denial paths and integration behavior, not actual independent acceptance. No real proof,
reviewer enrollment, profile activation or external side effect is produced by this implementation.
