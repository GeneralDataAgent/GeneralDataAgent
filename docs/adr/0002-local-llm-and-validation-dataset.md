# ADR 0002 CC Switch LLM and validation dataset

## Status

Accepted for the second implementation increment.

## Decision

Use an Anthropic-compatible provider already managed by the user's local CC Switch database for the
first real-model integration. Resolve the current provider at runtime without copying its credential
into this repository. Restrict the model to evidence-bounded structured narrative generation;
deterministic components continue to own SQL compilation, policy, execution, evidence capture, and
validation. The same LangChain chat model can later be supplied to the Deep Agents supervisor.

Use the NYC Taxi and Limousine Commission January 2025 green-taxi trip records as the first real
validation source. Download the official Parquet trip file and taxi-zone lookup, record SHA-256
hashes and source URLs, transform them into a curated local SQLite snapshot, and query that snapshot
through the existing read-only executor.

## Rationale

CC Switch already manages several model providers, base URLs, model identifiers, and credentials.
Reading its selected provider in read-only mode avoids a second secret store and preserves the
user's provider-switching workflow. Keeping the model away from query execution preserves the
existing governance controls while proving a real inference path.

TLC publishes actual vendor-reported trip records, a field dictionary, monthly Parquet files, and a
zone lookup. The January green-taxi file is small enough for a repeatable local acceptance run while
still containing realistic timestamps, geography, distance, passenger, payment, and fare fields.

## Consequences

- Local LLM runs require a selected Anthropic-compatible provider in CC Switch.
- CI uses a fake narrator and never receives a real credential.
- Model output is stored as a separate artifact and labeled with the resolved model.
- Downloaded source files and generated databases remain outside Git.
- The source disclaimer and limitations must be retained in validation documentation.
- Tool-calling through the Deep Agents supervisor must be covered by a separate acceptance suite;
  this increment validates structured model inference without granting data tools to the model.
