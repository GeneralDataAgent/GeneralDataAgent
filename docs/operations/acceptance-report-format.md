# Acceptance report format

Use this format when recording a phase acceptance decision. It is a reporting procedure, not a completed report. Store filled reports and machine evidence in the [external validation directory](../../../docs/validation/README.md); apply the [shared criteria](../../../docs/planning/phase-acceptance.md) and the relevant phase plan.

```text
Phase and version:
Environment and data snapshot:
Model, prompt, Skill, tool, and policy versions:
Evaluation cohort version and sample count:
Blocking-check results:
Core metric results:
Failure cases and root causes:
Security and fault-injection results:
Cost and performance:
Known limitations:
Outstanding risks and owners:
Admission to the next stage:
Approver and date:
```

Retain complete case-level failures and missing evidence. Separate local implementation checks, benchmark quality, independent external qualification, and production admission. A merged commit or passing local suite is not an approval to enter the next environment.
