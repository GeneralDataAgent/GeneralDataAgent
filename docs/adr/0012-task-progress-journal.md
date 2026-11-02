# ADR 0012: Keep application progress separate from execution and audit evidence

Status: Accepted for a local opt-in pilot, 2026-11-02.

## Context

Web and messaging consumers need resumable progress. The existing synchronous analysis API and
protected checkpoints do not expose a stable, safe application event protocol. Raw graph streaming
can include prompts, tool arguments or data; the audit sink also serves a different purpose.

## Decision

Add a versioned metadata-only `TaskEventJournal`, written by the actual metric graph and read through
the existing authenticated API. SQLite transactions assign task-local sequences and prevent writes
after a terminal event. Reads require the creator, tenant, project and original domains. Events have
expiry and bounded pages; the journal is separate from artifacts and unavailable to agent tools.

Use fixed phases and outcomes rather than generic payload dictionaries. Expose finite cursor-based
JSON first. Asynchronous task acceptance, task/thread mapping, live SSE, cancellation and additional
runtimes follow as separate slices. All transports will consume the same application events.

## Consequences

Opted-in executions fail on journal errors. Checkpoints remain authoritative for node recovery;
events describe observations and allow repeated phases after retry. Resume can reconcile a completed
checkpoint missing its terminal event. There is no atomic transaction across progress, checkpoints,
audit, artifacts or external side effects, and no exactly-once execution claim.

The local adapter cannot be enabled in production constructors. Existing assemblies without a
journal retain their behavior. A future production adapter needs explicit maintenance, identity,
retention and recovery qualification. This ADR does not change existing release gates.
