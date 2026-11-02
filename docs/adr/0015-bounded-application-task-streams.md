# ADR 0015: Stream persisted application metadata through live authorization

Status: Accepted for the local task adapter, 2026-11-03.

## Context

Cursor polling can recover history but does not provide a live workbench view. Forwarding the model
or graph protocol would expose internal state, and authentication only at connection time would keep
revoked sessions active. Unbounded producer queues would accumulate work for slow clients.

## Decision

Expose bounded SSE over existing queue metadata and the persisted progress journal. Bind progress IDs
to task and sequence, preserve JSON cursors, emit separate current-status snapshots, and drain the
journal before closing a terminal task. Recheck live authorization, scope, retention and the service
gate while streaming. Sanitize post-header failures into a fixed versioned end frame.

Apply operator-owned frame/duration bounds and per-process/principal admission; poll only as the
consumer pulls. Release admission on disconnect/close and reap abandoned setup. Keep task execution
independent: disconnecting a viewer is not a cancellation request.

## Consequences

The actual loopback client observes queued status before a worker is released and can reconnect with
no silent progress gaps. Status snapshots and replayed phase history remain distinct. Socket-send
timeouts, distributed admission, full stream SLO and production acceptance require later deployment
work. This slice implements SSE only; explicit cancellation remains separate.
