# Evaluation execution receipts

Fresh research runs can opt in to `ExecutionReceipt` output with an explicitly
supplied operator identity:

```bash
uv run python evals/run_stage6.py \
  --benchmark infi --limit 10 --evaluation-protocol stage7-v1 \
  --tool-profile sql-semantic \
  --receipt-actor '{"user_id":"operator","tenant_id":"research","project_id":"evaluation","data_domains":["public"]}' \
  --output .artifacts/receipt-run
```

Use the existing model/provider options for the intended admitted evaluation
route. Identity here is an operator's research declaration, not an authenticated
production identity. The feature uses `LocalArtifactStore` and makes no gateway,
production admission or independent verification claim.

The runner assigns each attempted case a new task UUID and trace UUID. Successful
tool steps persist their exact result JSON under the run's `artifacts/` directory,
then compare readback with the intended bytes. The serializer preserves the
legacy `result_hash` algorithm, so answer bindings keep their existing hashes.
Reports add `receipt_context` and `execution_receipts`; each entry links a step,
legacy method identifier and hash to its own execution receipt. The receipt
references one same-task result artifact. Sources include the database file,
documents and full public task; no golden answer or reference SQL is supplied.

Verification is `producer_self_check`, with named result-readback and step-hash
checks. It does not assert answer correctness, statistical adequacy or independent
business verification. Step token/cost/elapsed measurements remain unknown; one
tool call is observed. Existing per-case model usage remains on the case report
and is not duplicated across step receipts. An explicit `complete=false` output
stays partial. A recorded execution may belong to a failed case.

`validate_evaluation_receipts(row)` checks declared identity/source/method/hash
agreement, trace step coverage and field binding links. Supply
`read_bytes=store.read_bytes` to additionally rehash stored bytes and validate raw
answer paths. Metadata-only validation cannot inspect remote artifacts or certify
semantics. Stage 7 qualification requires artifact readback for new receipt rows;
pass `--artifact-root` (one or more run artifact directories) to
`evals/qualify_stage7.py`. Missing or corrupt artifacts block qualification, while
legacy rows keep their previous checks. SQL dependency and method checks remain,
and malformed new envelopes are rejected on failed cases too.

Without `--receipt-actor`, result shapes retain the existing fields. Legacy rows
without the two new fields return an empty receipt tuple when validated, are not
rewritten, and receive no fabricated actor, source, usage or verification scope.
New opt-in runs carry an adapter/contract fingerprint in their system manifest;
they require fresh comparison arms when system fingerprints differ.

Artifacts and reports must stay together for readback. Recording failures stop the
worker rather than consuming model repair attempts; the existing isolated-worker
failure report still records that the worker did not return complete telemetry.
This is a static development database-file binding: active nonempty SQLite WAL
files are refused, and recorder inputs must match the actual run before model
access. It is not an atomic database snapshot or a proof of authenticity.
