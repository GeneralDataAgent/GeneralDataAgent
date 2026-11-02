# ADR 0017: Consume confirmed private preferences on completed-result presentation

Status: Accepted for the local adapter, 2026-11-03.

Business memory must have explicit provenance and authorization, and must not silently change a
queued task's accepted inputs on restart. Start with a bounded private display choice and a real
read-side consumer, rather than introducing free-form context into certified metric execution.

Persist explicitly confirmed layout/preview choices in a dedicated AES-GCM store with exact actor
scope, CAS revisions, server-issued source IDs, validity and hashes. Recheck live grants, scan typed
content and require audit intents before state changes. Revocation removes a value from new active
retrieval but retains separately authenticated historical citations.

Add a separate versioned presentation endpoint over the existing completed-result evidence reader.
Each presentation binds its source task/result hash and complete preference revision. Only layout
and preview size change; certified calculation, evidence, original result, queue/checkpoint contracts
and recovery inputs remain unchanged. Concurrent preference changes fail the read instead of mixing
versions. Execution-time context injection requires a later acceptance protocol.

This adapter is disabled by default and refused by production constructors. Append history is bounded,
not a production memory service or physical erasure mechanism. Local audit intents are not transaction
receipts or independent signed evidence; no business benefit or model-quality improvement is claimed.
