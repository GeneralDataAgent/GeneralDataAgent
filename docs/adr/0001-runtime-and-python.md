# ADR 0001 Runtime and Python baseline

## Status

Accepted for the initial implementation.

## Decision

Use Python 3.12 managed by `uv`. Use Deep Agents 0.7, LangChain 1.x, and LangGraph 1.x through bounded dependency ranges. Commit the generated lockfile. Keep model-provider packages out of the base dependency set until a provider and data-processing agreement are selected.

## Rationale

The current Deep Agents package requires Python 3.11 or later. Python 3.12 is a conservative common baseline for the SDK, tooling, and intended deployment images. Bounded ranges allow compatible patches while preventing an unreviewed major upgrade.

## Consequences

- Developers must use `uv sync` rather than the system Python environment.
- Every dependency update runs the complete deterministic test and evaluation suite.
- Model-provider integration is a separate ADR and cannot silently enter the base package.
