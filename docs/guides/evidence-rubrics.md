# Versioned evidence rubrics (Stage 14.1A)

`TaskRubric` is a deterministic, versioned acceptance declaration for **one task**. It is a library
port: there is no API route, model call, automatic repair, approval, audit write or production mode
change. A rubric states exactly which sources, method, time range, claims, table/chart evidence and
human-confirmation items are required, and maps every item to a named registered verifier.

## Build a rubric

```python
from uuid import uuid4

from generaldataagent.contracts.rubric import (
    RubricDeliveryRequirement,
    RubricMethodRequirement,
    RubricSourceRequirement,
    TaskRubric,
    rubric_payload,
)
from generaldataagent.memory.display_preferences import canonical_hash

task_id = uuid4()
sources = (
    RubricSourceRequirement(source_id="orders", content_hash="sha256:<64 hex>", version="v1"),
)
method = RubricMethodRequirement(method_id="metric.certified", method_version="v1")
delivery = RubricDeliveryRequirement(
    required_claim_types=("fact", "limitation"),
    human_confirmation_items=("business_owner_signoff",),
    verifier_ids=(
        "rubric.required_checks_present",
        "rubric.time_range_bound",
        "rubric.metric_version_bound",
        "rubric.sources_single_snapshot",
        "rubric.claims_grounded",
        "rubric.table_and_chart_evidence",
        "rubric.human_items_acknowledged",
    ),
)
payload = rubric_payload(
    rubric_id="weekly-report",
    task_id=task_id,
    metric_name="weekly_sales",
    metric_version="v1",
    start_time="2026-11-01",
    end_time="2026-11-02",
    sources=sources,
    method=method,
    delivery=delivery,
)
rubric = TaskRubric(
    **{
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "sources", "method", "delivery"}
    },
    sources=sources,
    method=method,
    delivery=delivery,
    rubric_digest=canonical_hash(payload),
)
```

`rubric_digest` binds the entire declaration. v1 requires the complete set of seven registered
verifiers; unknown, duplicate, incomplete or omitted IDs, unparseable/inverted ISO time ranges,
duplicate sources/human items, or a changed field with an old digest are refused. The IDs and their
deterministic checks are listed in [ADR 0023](../adr/0023-versioned-evidence-rubrics.md).

## Evaluate one delivery

```python
from generaldataagent.review.rubric import evaluate_rubric

result = evaluate_rubric(
    rubric=rubric,
    package=delivery_package,  # DeliveryPackage for the same task
    sources=source_bindings,  # observed SourceBinding tuple
    receipt=execution_receipt,  # ExecutionReceipt for the same task
)
print(result.verdict, result.evaluation_digest)
```

The evaluator refuses any task mismatch, unverifiable digest, unknown verifier, inconsistent
source/method verifier declaration, duplicate source binding, conflicting evidence binding or receipt
result reference that is not declared delivery evidence. It then runs every declared verifier. Results
are explicit:

| Outcome | Meaning |
|---|---|
| `satisfied` | The named deterministic check passed |
| `gap` | Source/evidence/method/claim/time-range fact is missing or inconsistent |
| `needs_human` | A named human attestation is required, or its check codes do not name every human item; **never** a pass |

`verdict=fail` if any gap exists; otherwise `needs_human` if a human item is outstanding; otherwise
`pass`. Every evaluation fixes `producer_self_check_only=true`, `model_judgement_used=false` and
`revision_performed=false`, and carries its own digest. Model comments cannot override a
deterministic failure, and an evaluation never rewrites an older rubric or delivery.

## Limits

This slice does not bind business templates, perform the Stage 14 bounded revision loop, display a
UI, integrate with the metric graph, write approvals/audit or change production admission. A
`human_attestation` receipt is an attestation recorded by an existing authority, not independent
truth or business correctness. `independent_review=not_run`, `production_accepted=false`.

```bash
uv run pytest tests/test_stage14_task_rubric.py
```
