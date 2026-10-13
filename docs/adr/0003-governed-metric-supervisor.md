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

Real-model testing exposed two failure modes that prompt-only coordination did not prevent:

1. The model invented a task UUID when delegating to Verifier.
2. The model stopped after Verifier and claimed completion without calling Reporter.

Binding Verifier and Reporter to the single controller-owned candidate removes the first failure.
A bounded second Supervisor invocation was tested for the second failure, but it ran for more than
eleven minutes before manual interruption. Reporter is an assembly step over validated data, so a
fixed node is both safer and consistent with the planned hybrid of dynamic planning and fixed
control gates.

## Consequences

- The LLM chooses and coordinates evidence-producing work but cannot bypass authorization or
  verification.
- Discovery receives catalog metadata only; Data Analyst receives the certified executor only;
  Verifier receives validation state only; Reporter receives validated delivery only.
- General filesystem, shell, and general-purpose subagent capabilities are disabled for this path.
- Repeated Data Analyst calls in one request are idempotent and return the same candidate.
- Provider exceptions are converted into a structured failed result. The acceptance profile uses
  no automatic provider retry so rate limits fail quickly instead of holding the workflow open.
- This increment validates the analytics slice. Data Engineer and Data Scientist orchestration
  remain subsequent increments.
- The current free CC Switch model is suitable for functional exploration, not latency acceptance.
