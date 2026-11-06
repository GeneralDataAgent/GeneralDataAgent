# ADR 0024: Bind business report shapes to complete rubrics, never to prompts

Status: accepted for three fixed local templates; business workflow and production admission unchanged.

## Context

Stage 14.1B needs rubric declarations for three recurring report shapes: the weekly business
report, channel change and anomaly list. A template could easily become a mutable prompt that
silently drops a required claim, human signoff or verifier. The existing `TaskRubric` already fixes
the acceptance checks; the missing piece is a deterministic way to bind a named business shape to
those checks without resolving metric aliases or accepting caller-supplied weakening.

## Decision

Add v1 `BusinessRubricTemplate` with a fixed template ID, template version, minimum source count,
required claim types, human-confirmation items, the complete seven-verifier set and a canonical
template digest. Construction refuses an incomplete verifier set, unknown/duplicate verifiers,
duplicate claim types or human items, a template without `fact` and `limitation`, and a digest
mismatch.

The repository owns exactly three templates:

| Template | Minimum sources | Required claims | Human item |
|---|---|---|---|
| `business.weekly_report` | 1 | `fact`, `interpretation`, `limitation` | `business_owner_signoff` |
| `business.channel_change` | 2 | `fact`, `interpretation`, `limitation` | `channel_owner_signoff` |
| `business.anomaly_list` | 1 | `fact`, `recommendation`, `limitation` | `operations_owner_signoff` |

`bind_business_template` accepts exact task facts, exact `RubricSourceRequirement` values, an
`RubricMethodRequirement` and optional additional claims/human items. It adds every template
requirement to the resulting `TaskRubric`; it never accepts a caller list that could remove one.
Unknown templates or claim types, duplicate sources/claims/human items, a source/method verifier
mismatch and a source count below the template minimum are refused before a rubric exists. The
resulting `rubric_id` starts with the template ID and includes the task and metric version.

Metric alias resolution remains upstream. The binding takes an already-resolved metric name/version
and never guesses between ambiguous aliases; ambiguity is refused before this function is called.
Template requirements do not become model instructions, and the template itself performs no model
call, write, repair or revision.

## Consequences

Operators can build a complete rubric for each recurring report shape without copying a checklist
or trusting a prompt. The three human-signoff items mean a bound rubric evaluates to `needs_human`
until the named attestation covers the item; the template never turns that into a pass. The
template digest and the resulting rubric digest make both the shape and the exact task facts
auditable. The Python checks remain deterministic and independent of report prose.

This slice does not resolve metrics, call a model, expose an API or UI, write approvals or audit,
perform the Stage 14 bounded revision, bind a live business workflow or change production
admission. `independent_review=not_run`, `production_accepted=false`.
