import pytest
from pydantic import ValidationError

from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition, MetricNotFoundError


def metric() -> MetricDefinition:
    return MetricDefinition(
        name="order_count",
        version="v1",
        description="Number of accepted orders",
        data_domain="commerce",
        relation="orders",
        expression="COUNT(*)",
        time_column="created_at",
        allowed_dimensions=("channel",),
        owner="commerce-data",
    )


def test_catalog_resolves_exact_metric_version() -> None:
    catalog = InMemoryMetricCatalog([metric()])
    assert catalog.get("order_count", "v1").qualified_name() == "order_count@v1"
    with pytest.raises(MetricNotFoundError):
        catalog.get("order_count", "v2")


def test_metric_rejects_unsafe_dimension_identifier() -> None:
    values = metric().model_dump()
    values["allowed_dimensions"] = ('channel"; DROP TABLE orders; --',)
    with pytest.raises(ValidationError, match="invalid dimension"):
        MetricDefinition(**values)
