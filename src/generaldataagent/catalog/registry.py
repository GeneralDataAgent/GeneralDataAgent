"""Small catalog adapter; production will wrap the enterprise semantic layer."""

from collections.abc import Iterable

from generaldataagent.catalog.models import MetricDefinition
from generaldataagent.catalog.resolver import MetricMatch, find_metric_matches


class MetricNotFoundError(LookupError):
    pass


class InMemoryMetricCatalog:
    def __init__(self, metrics: Iterable[MetricDefinition]) -> None:
        self._metrics: dict[tuple[str, str], MetricDefinition] = {}
        for metric in metrics:
            key = (metric.name, metric.version)
            if key in self._metrics:
                raise ValueError(f"duplicate metric definition: {metric.qualified_name()}")
            self._metrics[key] = metric

    def get(self, name: str, version: str) -> MetricDefinition:
        try:
            return self._metrics[(name, version)]
        except KeyError as exc:
            raise MetricNotFoundError(f"unknown certified metric: {name}@{version}") from exc

    def list(self) -> tuple[MetricDefinition, ...]:
        return tuple(sorted(self._metrics.values(), key=lambda item: (item.name, item.version)))

    def search(
        self,
        question: str,
        *,
        metric_version: str | None = None,
        data_domains: tuple[str, ...] | None = None,
    ) -> tuple[MetricMatch, ...]:
        return find_metric_matches(
            self.list(),
            question,
            metric_version=metric_version,
            data_domains=data_domains,
        )
