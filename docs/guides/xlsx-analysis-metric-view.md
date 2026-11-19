# XLSX analysis metric view (non-certified)

The local XLSX analysis binder can compute one exact integer aggregate over one creator-bound
analysis artifact. This is the first, deliberately narrow link from a retained analysis dataset to a
metric-shaped result view. It is **not** a certified metric query: no metric catalog entry is
resolved, no `DatasetSnapshot` is created, and the response is not a task result or delivery package.
It exists so a caller can inspect a single exact aggregate without exposing cell values, while the
authorization, hash-binding, DLP, audit and budget boundaries stay explicit.

The port remains local and development-only. `create_app(production_mode=True, xlsx_analysis=...)`
is not a supported production route.

## Assembly

Pass a dedicated `TaskBudgetLedger` to the binder. Its database and the artifact index must stay
outside the artifact content tree:

```python
binder = XlsxAnalysisBinder(
    intake=intake,
    budgets=TaskBudgetLedger("/private/operator/xlsx-analysis-budgets.db"),
    artifact_root="/private/operator/xlsx-analysis-artifacts",
    artifact_index="/private/operator/xlsx-analysis-index.db",
)
create_app(session_broker=broker, xlsx_intake=intake, xlsx_analysis=binder)
```

The binder shares the intake identity broker and fails closed on construction when the ledger or
index sits inside the artifact root.

## Request

```http
GET /v1/analysis/files/xlsx/{file_id}/snapshot/analysis/{artifact_id}/metric
    ?snapshot_hash=sha256:<64 hex>
    &metric=row-count|distinct-count|integer-sum|integer-min|integer-max
    &column=<zero-based declared column index>
```

`column` is required for every metric except `row-count`, and forbidden for it. The artifact must
be a `xlsx_analysis_dataset` created by the same task, located by exact artifact ID and artifact
type. `snapshot_hash` must still be the current declaration's snapshot.

## Output

The response carries the artifact ID and content hash, the snapshot hash, the metric, the selected
column descriptor (or `null` for `row-count`), the exact integer `value` and the artifact's
`row_count`. It never contains a cell value. Fixed markers state the limits of the view:

| Field | Value |
|---|---|
| `scope` | `one-creator-bound-analysis-artifact` |
| `aggregation` | `exact-integer-single-pass` |
| `qualification` | `non-certified-analysis-view` |
| `metric_catalog` | `not-registered` |
| `metric_definition` | `not-resolved` |
| `result_view` | `analysis-metric-view-not-metric-result` |
| `storage` | `dlp-scanned-creator-artifact` |
| `row_scope` | `present-value-only` |
| `roles` / `target` | `not-assigned` / `not-designated` |
| `dataset_snapshot` | `false` |
| `certified_metric_source` | `false` |

`integer-sum`, `integer-min` and `integer-max` are available only for columns whose analysis type is
`integer`. Decimal and date columns stay canonical text; the service does not coerce them into
numbers. `distinct-count` counts exact typed values for any bound column. `row-count` reports the
artifact's retained present rows and discloses no header text.

## Boundaries

- **Authorization** reuses the intake guard: a live opaque session with `analysis:run`, no business
  constraint, the exact creator user/tenant/project and the actor's data domains, plus the retained
  file's expiry.
- **Hash binding** requires the artifact reference's content hash to match the bytes read, the
  artifact's task ID to match the reference, and the artifact's file ID and snapshot hash to match
  the current snapshot. The current snapshot is re-read after the audit.
- **DLP** rescans the one disclosed header before returning a column metric. `row-count` discloses
  no text and reports `dlp=no-text-disclosed`.
- **Audit** writes `xlsx.analysis_metric_read` with the snapshot hash, artifact ID, metric, column
  index, value and row count. An unavailable audit withholds the response.
- **Budget** registers the file's task in the dedicated ledger with the intake operation limit and
  reserves one bounded call plus the artifact's byte size before reading it. An exhausted or
  conflicting budget denies the request before any artifact bytes are read.
- A stale declaration, changed snapshot, foreign creator, unknown artifact or expired file yields
  the same fail-closed errors as the analysis values and profile reads.
- This view does not add filtering, grouping, joins, cross-artifact aggregation, time windows,
  certified metric definitions, task results, delivery or model tools.

`independent_review=not_run` and `production_accepted=false` remain the external status. A matching
local aggregate is not evidence of metric certification or production acceptance.
