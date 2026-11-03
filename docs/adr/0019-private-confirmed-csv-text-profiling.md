# ADR 0019: Confirm private CSV text snapshots before governed profiling

Status: Accepted for the local adapter, 2026-11-03.

SME file intake needs a real consumer without accepting server paths, guessing types or publishing
uncertified metrics. A raw upload must not silently become an authorized analytics dataset.

Accept bounded UTF-8 CSV bytes into an encrypted creator/project/domain-private store. Pin source,
parser/Python/limits and processing release, text-only config and preview hashes. Require explicit
current-session confirmation of source and preview before materializing a temporary all-TEXT table.
Preserve empty strings/leading zeroes/duplicates and report text semantics explicitly. Refuse parser
or processing drift rather than reinterpret stored inputs.

Reuse DataEngineeringRuntime/ToolGateway/read-only native connector profiling with budget, audit,
DLP and creator-only governed evidence. Quote identifiers, bind data values and close connections.
Persist a verified encrypted result atomically in the intake DB; repeats read that result without SQL.
Keep execution-budget usage/deadline on failures. Ordinary rollback discards new artifacts and scratch,
but separate audit/ledger/artifact stores do not provide crash atomicity or exactly-once execution.

Default this capability off and reject it in production constructors. Input/row/column/cell/time and
retained-file bounds make a deterministic local pilot. Numeric/Excel interpretation, source cleanup,
sharing, unified async tasks, raw download and production qualification remain separate slices.
