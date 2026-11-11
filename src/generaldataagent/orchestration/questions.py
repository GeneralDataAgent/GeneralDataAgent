"""Deterministic question resolution and certified-query construction.

The synchronous question endpoint and durable question-task acceptance share this module so a
submitted question resolves to exactly the same certified query in both paths. Nothing here logs,
persists or returns the question body on its own.
"""

from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import (
    ActorContext,
    MetricCandidate,
    MetricQuery,
    MetricResolution,
    NaturalLanguageMetricQuery,
)


def resolve_question(
    catalog: InMemoryMetricCatalog,
    request: NaturalLanguageMetricQuery,
    actor: ActorContext,
) -> MetricResolution:
    """Resolve a question only against metrics the actor's data domains authorize."""
    matches = catalog.search(
        request.question,
        metric_version=request.metric_version,
        data_domains=actor.data_domains,
    )
    if not matches:
        return MetricResolution(
            status="not_found",
            question=request.question,
            clarification_question=(
                "No authorized certified metric matched. Specify the metric name or business "
                "definition."
            ),
        )
    top_score = matches[0].score
    top_matches = tuple(item for item in matches if item.score == top_score)
    candidates = tuple(
        MetricCandidate(
            metric_name=item.metric.name,
            metric_version=item.metric.version,
            description=item.metric.description,
            unit=item.metric.unit,
            matched_alias=item.matched_alias,
        )
        for item in top_matches
    )
    if len(candidates) > 1:
        choices = ", ".join(f"{item.metric_name}@{item.metric_version}" for item in candidates)
        return MetricResolution(
            status="ambiguous",
            question=request.question,
            candidates=candidates,
            clarification_question=f"Choose one certified metric: {choices}.",
        )
    return MetricResolution(
        status="resolved",
        question=request.question,
        candidates=candidates,
    )


def validate_metric_query(metric: MetricDefinition, query: MetricQuery) -> None:
    """Reject query options the certified definition does not authorize."""
    invalid = set(query.dimensions) - set(metric.allowed_dimensions)
    if invalid:
        raise ValueError(f"dimensions are not certified for this metric: {sorted(invalid)}")
    if query.start_time is not None and metric.time_column is None:
        raise ValueError("this metric does not define a certified time column")


def certified_query(
    catalog: InMemoryMetricCatalog,
    resolution: MetricResolution,
    request: NaturalLanguageMetricQuery,
) -> tuple[MetricQuery, MetricCandidate]:
    """Turn a resolved question into the exact query that will be durably accepted."""
    if resolution.status != "resolved":
        raise ValueError("question is not resolved to one certified metric")
    candidate = resolution.candidates[0]
    metric = catalog.get(candidate.metric_name, candidate.metric_version)
    query = MetricQuery(
        metric_name=metric.name,
        metric_version=metric.version,
        dimensions=request.dimensions,
        start_time=request.start_time,
        end_time=request.end_time,
    )
    validate_metric_query(metric, query)
    return query, candidate
