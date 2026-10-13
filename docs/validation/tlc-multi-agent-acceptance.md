# NYC TLC multi-agent acceptance record

## Scope

This record covers the Stage 1 certified-metric slice using the January 2025 NYC TLC green-taxi
snapshot and Anthropic-compatible or OpenAI Responses-compatible models selected by stable CC
Switch provider ID. The deterministic data result and source hashes remain those recorded in
`tlc-llm-acceptance.md`.

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
| 8 | An OpenRouter Anthropic-compatible model returned valid structured output in the adapter smoke test | Provider selection and secret-safe loading succeeded in 10.42 seconds |
| 9 | OpenAI Responses adapter initially used the CC Switch OpenRouter `/api` base | Base URL was normalized to `/api/v1`; the structured smoke test succeeded in 10.18 seconds |
| 10 | Full OpenAI Responses multi-agent run remained in nested tool execution for more than four minutes | Run was interrupted; a whole-workflow deadline was added and covered by an async timeout test |
| 11 | Latest OpenAI Responses real-provider run was rate limited | Workflow returned structured `failed` in 2.69 seconds with no retry, role, or artifact leakage |

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
- A whole-workflow deadline cancels an over-budget async Supervisor execution and returns a
  structured failure.
- The repository test suite contains 71 passing tests with 89% line coverage.

## Performance status

**Not accepted.** Observed LLM-led attempts took roughly 165 to 300 seconds, and two experiments
exceeded 673 seconds before interruption. This is above the planned Stage 1 P95 target of 60
seconds to a first useful result. The acceptance profile now uses a 30-second per-request timeout,
no provider retry, and a 60-second whole-workflow budget. Provider errors and workflow timeouts are
converted into structured failures. On the latest run, the selected OpenRouter Responses provider
returned `OpenAIRateLimitError`; the workflow failed closed in 2.69 seconds. Because the provider
failed before tool execution, this run validates the provider-error path, not the real-provider
60-second deadline path.

## Remaining acceptance action

Repeat the updated hybrid flow when a tool-calling provider is available without rate limiting.
The real-model workflow is functionally accepted only when it returns `completed`, the exact
four-role sequence, and immutable evidence URIs. Run enough successful tasks to calculate the
planned success rate and P95 rather than treating a smoke test as acceptance. The fixed Reporter,
provider-failure, and total-deadline behaviors are covered by automated tests; real-provider
success and performance remain open gates.
