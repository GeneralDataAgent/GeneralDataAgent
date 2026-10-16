# ADR 0005: Fixed-template data-science sandbox

Status: Accepted for the Stage 3 repository implementation on 2026-10-15.

## Context

Stage 3 needs reproducible EDA, statistical checks, baseline training, evaluation, and error
analysis. Accepting arbitrary Python from an agent would create an uncontrolled code-execution and
data-exfiltration boundary before the production sandbox, identity, and network controls in Stage 4
exist.

## Decision

The local workbench runs only a repository-owned, versioned Python template. Callers provide a
strict `ExperimentSpec`; there is no code, command, package, import, path, or environment field.
The worker receives canonical snapshot rows over standard input and runs in isolated Python mode
with site loading disabled, a temporary working directory, a minimal environment, wall-clock
timeout, CPU limit, address-space limit, file-size limit, descriptor limit, and input/output byte
limits. The runtime fingerprint binds the worker bytes and complete Python runtime version.

The template implements deterministic majority-class and mean-regression baselines, EDA, held-out
evaluation, target distributions, standardized mean differences, and error analysis. Split indices
are created outside the worker and validated before execution. Exact target proxies, cross-split
duplicates, malformed targets, incorrect split roles, metric misuse, and task/snapshot mismatches
fail closed.

Model registration is a separate R2 action. It creates only a draft, consumes an exact one-time
approval from a reviewer other than the requester, and cannot express or perform deployment.

## Consequences and boundary

- Experiments are reproducible and do not execute agent-authored code or install dependencies.
- The standard-library-only dependency surface is fixed by the repository lock and runtime
  fingerprint.
- This process boundary is suitable for deterministic local development, not a substitute for a
  hardened container or microVM. Network namespace isolation, read-only images, seccomp, workload
  identity, and an independent security review remain Stage 4 production prerequisites.
- Adding an algorithm requires a reviewed template change, new frozen cases, and a new runtime
  fingerprint.
