import pytest
from pydantic import ValidationError

from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition, MetricNotFoundError


def metric() -> MetricDefinition:
    return MetricDefinition(
        name="order_count",
        version="v1",
        description="Number of accepted orders",
        aliases=("order count", "orders"),
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


def test_catalog_resolves_unique_alias_and_preserves_ambiguity() -> None:
    average = metric().model_copy(
        update={
            "name": "average_order_value",
            "description": "Average accepted order value",
            "aliases": ("average order value", "orders"),
            "expression": "AVG(amount)",
            "unit": "USD",
        }
    )
    catalog = InMemoryMetricCatalog([metric(), average])

    unique = catalog.search("show average order value by channel", data_domains=("commerce",))
    assert len(unique) == 1
    assert unique[0].metric.name == "average_order_value"

    ambiguous = catalog.search("orders", data_domains=("commerce",))
    assert len(ambiguous) == 2
    assert ambiguous[0].score == ambiguous[1].score


def test_catalog_search_does_not_expose_unauthorized_metrics() -> None:
    catalog = InMemoryMetricCatalog([metric()])
    assert catalog.search("orders", data_domains=("finance",)) == ()
