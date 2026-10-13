# NYC TLC multi-agent acceptance record

## Scope

This record covers the Stage 1 certified-metric slice using the January 2025 NYC TLC green-taxi
snapshot and the current Anthropic-compatible model selected by CC Switch. The deterministic data
result and source hashes remain those recorded in `tlc-llm-acceptance.md`.

## Architecture exercised

| Role | Invocation | Permitted capability |
|---|---|---|
| Supervisor | LLM / Deep Agents | Delegate named specialists; no data or filesystem tools |
| Discovery | Dynamic specialist | Read one authorized certified metric definition |
| Data Analyst | Dynamic specialist | Execute one certified metric through governed runtime |
| Verifier | Dynamic specialist | Read immutable deterministic validation checks |
| Reporter | Fixed post-validation node | Read the validated delivery package only |

Controller state, rather than the model, owns the analysis candidate, task ID, role ledger, and
final evidence URIs.

## Real-model findings

| Attempt | Observed behavior | Control response |
|---|---|---|
| 1 | Model invented a different task UUID for Verifier | Unknown candidate was rejected; tools were rebound to the request candidate |
| 2 | Discovery stopped because metadata was insufficient | Certified semantic and execution contract fields were exposed |
| 3 | Model completed Discovery, Analyst, and Verifier but skipped Reporter | Deterministic role ledger rejected completion |
| 4 | Missing Reporter was reported as a failed governed sequence | Fail-closed behavior confirmed |
| 5 | Same-thread LLM repair exceeded 11 minutes | Repair was interrupted and replaced with a fixed Reporter node |
| 6 | Updated hybrid flow remained inside provider retry handling for 678.47 seconds | Run was interrupted; client retries were disabled for the acceptance profile |
| 7 | Provider returned a rate-limit response | Workflow returned a structured `failed` result in 2.28 seconds with no partial roles or artifacts |

## Automated acceptance

- Actor data-domain scope is enforced during Discovery.
- Uncertified dimensions are rejected.
- Out-of-order specialist tool calls are rejected.
- Repeated analysis is idempotent within a request.
- Verifier fails closed without a candidate.
- Reporter cannot read a candidate until validation passes.
- A verified candidate missing only Reporter is finalized once through the fixed Reporter node.
- Final identifiers and evidence references are overwritten from controller-owned state.
- Model-provider exceptions fail closed without exposing credentials or accepting partial output.
- The repository test suite contains 47 passing tests with 89% line coverage.

## Performance status

**Not accepted.** Observed LLM-led attempts took roughly 165 to 300 seconds, and two experiments
exceeded 673 seconds before interruption. This is above the planned Stage 1 P95 target of 60
seconds to a first useful result. The acceptance profile now uses a 30-second per-request timeout
with no provider retry and converts provider errors into a structured failure. On the latest run,
the selected `z-ai/glm-5.3-flash-free` provider returned `AnthropicRateLimitError`; the workflow
failed closed in 2.28 seconds.

## Remaining acceptance action

Repeat the updated hybrid flow when a tool-calling provider is available without rate limiting. The
real-model workflow is functionally accepted only when it returns `completed`, the exact four-role
sequence, and immutable evidence URIs. The fixed Reporter behavior and provider-failure behavior
are covered by automated tests; real-provider success and performance remain open gates.
