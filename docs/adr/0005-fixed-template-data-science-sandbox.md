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

The production-oriented execution path uses an OCI image reference pinned by SHA-256 digest. Its
runner accepts only Docker or Podman and always requests an already-present image (`pull=never`),
no network, a read-only root filesystem, all capabilities dropped, no-new-privileges, a non-root
numeric user, PID/CPU/memory limits, and a small no-exec temporary filesystem. It exposes no volume,
environment, socket, or secret mount fields. The checked-in image contains only the fixed worker
and a digest-pinned Python 3.12.13 slim base.

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
- The local process boundary is suitable for deterministic development, not production isolation.
  The OCI path supplies a reviewable hardened invocation, but a real image build, runtime execution,
  seccomp/escape validation, workload identity, and an independent security review remain required.
- Adding an algorithm requires a reviewed template change, new frozen cases, and a new runtime
  fingerprint.
