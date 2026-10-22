# ADR 0003 Governed metric Supervisor and fixed Reporter gate

## Status

Accepted for the third implementation increment.

## Decision

Use Deep Agents for dynamic delegation of three evidence-producing roles in the certified-metric
path: Discovery, Data Analyst, and Verifier. Bind every specialist to an authenticated, minimal tool
surface created for one request. Keep the internal analysis candidate in controller-owned state;
the model cannot select or rewrite its task identifier.

After deterministic verification passes, invoke Reporter as a fixed control-flow node. Reporter can
only read the already-validated delivery package and cannot query data, alter validation, or create
new claims. A deterministic role ledger requires this exact sequence:

```text
Discovery -> Data Analyst -> Verifier -> Reporter
```

Any missing, duplicated out-of-order, or unauthorized role call fails closed. The final task ID,
role sequence, and artifact URIs are derived from controller state rather than trusted from model
output.

## Rationale

Prompt-only coordination permits an invented task identifier or completion before Reporter runs.
Binding Verifier and Reporter to the controller-owned candidate prevents identifier substitution.
A fixed Reporter node guarantees assembly after validation without another open-ended model loop.
The [historical validation record](../../../docs/progress/development-history.md) retains the experiments that motivated this decision.

## Consequences

- The LLM chooses and coordinates evidence-producing work but cannot bypass authorization or
  verification.
- Discovery receives catalog metadata only; Data Analyst receives the certified executor only;
  Verifier receives validation state only; Reporter receives validated delivery only.
- General filesystem, shell, and general-purpose subagent capabilities are disabled for this path.
- Repeated Data Analyst calls in one request are idempotent and return the same candidate.
- Provider exceptions are converted into a structured failed result. The acceptance profile uses
  no automatic provider retry so rate limits fail quickly instead of holding the workflow open.
- The decision covers the certified analytics slice. Other specialist workflows require separate
  orchestration contracts and qualification.
