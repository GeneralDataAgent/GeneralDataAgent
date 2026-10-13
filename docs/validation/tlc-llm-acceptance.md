# NYC TLC and CC Switch LLM acceptance record

## Run

- Date: 2026-10-13
- Source: NYC TLC January 2025 green-taxi trip records
- Raw rows curated: 48,326
- Rows inside the certified January time window: 48,283
- Model provider source: current Anthropic-compatible provider in CC Switch
- Resolved model: `z-ai/glm-5.3-flash-free`

## Source identity

- Trip SHA-256: `84f3a121667157efcbf012c3566a6065df6f8e0312c678cb2f29cd72cc9c0f10`
- Zone lookup SHA-256: `1a99e105092230f8620f301edcca7f80d3080642ff404d28ed957d3fa222c8ed`

## Certified result

| Pickup borough | Trip count |
|---|---:|
| Bronx | 570 |
| Brooklyn | 6,794 |
| Manhattan | 30,066 |
| N/A | 18 |
| Queens | 10,727 |
| Staten Island | 3 |
| Unknown | 105 |

## Controls verified

- CC Switch database opened read-only; the API credential was not printed, copied, or committed.
- SQL was compiled from a versioned metric definition and parsed by the read-only SQL guard.
- Data-domain and certified-dimension authorization ran before execution.
- SQLite opened the curated database in read-only mode.
- Query result and model narrative were stored as separate content-addressed artifacts.
- Delivery passed the non-bypassable validation gate.
- Audit events were written for analysis lifecycle and policy decision.

## Interpretation boundary

The model is used only for an evidence-bounded narrative after deterministic query execution. It
does not generate or execute SQL in this increment. Its output is descriptive, not causal. The raw
source disclaimer described in the dataset-selection document still applies.
