# ADR 0004: CC Switch model adapters and workflow deadlines

- Status: Accepted
- Date: 2026-10-13

## Context

Stage 1 must exercise real models without copying local credentials into source files. CC Switch
contains both Claude-style Anthropic-compatible providers and Codex-style providers that use the
OpenAI Responses protocol. Provider display names are not unique, individual request timeouts do
not bound a multi-step agent workflow, and OpenRouter's Responses endpoint requires an `/api/v1`
base path even when a stored CC Switch base path ends at `/api` (see the
[OpenRouter Responses API reference](https://openrouter.ai/docs/api/api-reference/responses/create-responses)).

## Decision

- Read the CC Switch SQLite database in read-only mode and retain credentials as secret values.
- Select providers by stable provider ID; names remain an optional convenience only when unique.
- Maintain separate Anthropic-compatible and OpenAI-compatible adapters behind the same
  `build_chat_model` boundary.
- Honor the stored Codex `wire_api` and enable the Responses API when requested.
- Normalize only OpenRouter's exact `/api` base path to `/api/v1`; do not rewrite other hosts.
- Apply both a provider-request timeout and a whole-Supervisor deadline.
- Disable retries for acceptance runs and convert provider errors or deadline expiry into a
  structured failed result without accepting partial role output.

## Consequences

The repository can validate different local CC Switch providers without persisting keys or relying
on ambiguous display names. The workflow has a controller-owned upper bound even when nested agent
tool execution performs multiple model calls. Rate limiting and timeout remain failed runs rather
than evidence of functional or performance acceptance, so successful repeated runs are still
required before Stage 1 can pass its real-model gates.
