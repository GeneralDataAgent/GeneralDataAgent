# ADR 0032: Deliver multiple evidence-bound charts

Status: Accepted for local development adapters, 2026-11-11.

## Context

`DeliveryPackage` v1 carries one `chart`. A result often benefits from more than one view of the
same rows, for example a dimension chart plus a precise value table. Producing separate result
packages or embedding copies of the rows would duplicate evidence and make consistency checks
ambiguous. The browser also renders exactly one chart model.

## Decision

Add an optional `charts` tuple to `DeliveryPackage`, bounded to six entries. `chart` remains
required and must equal the first entry when `charts` is present, so existing consumers can keep
reading one canonical chart. Entries must be unique by type, title, x field, y field and data
artifact URI. Every chart must reference an artifact that is already in the package evidence; charts
never embed rows or a second data copy.

The fixed metric runtime emits one KPI chart when the result has no dimension. With dimensions it
emits its existing bar or table chart and, when the result has rows, a second table view titled
`结果明细`. Production of a chart artifact is unchanged in kind: each chart gets its own
`chart_spec` artifact with lineage to the metric result. The result read verifies that the number of
chart artifacts, their unique contents and their URIs match the reconstruction exactly. The rubric
chart check requires evidence for every delivered chart.

The workspace validates `charts` fail-closed, requires its first entry to equal `chart`, rejects
duplicates, and renders each chart in the existing chart section. A legacy payload without `charts`
still renders its single `chart`. Rendering remains literal display of bound cells; no aggregation,
sorting or recalculation is added.

`schema_version` stays `1.0`: the field is additive and the single-chart meaning is unchanged, but
strict v1 readers that forbid unknown fields will reject a multi-chart package rather than ignore it
and misrender. Consumers must update in lockstep; no production qualification, independent
acceptance or new artifact body route is implied.

## Consequences

- Producers can describe multiple views without copying data or evidence.
- Existing single-chart producers remain valid by omitting `charts`.
- A package with a mismatched first chart, duplicate charts, too many charts, a chart outside
  evidence, or an artifact count/content mismatch fails closed.
- The API shape grows; generated clients and strict validators require regeneration and update.
