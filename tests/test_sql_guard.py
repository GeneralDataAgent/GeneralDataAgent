import pytest

from generaldataagent.tools import SqlGuard, SqlGuardError


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM analytics.orders",
        "UPDATE analytics.orders SET amount = 0",
        "INSERT INTO analytics.orders VALUES (1)",
        "DROP TABLE analytics.orders",
        "CREATE TABLE analytics.copy AS SELECT * FROM analytics.orders",
        "SELECT 1; SELECT 2",
    ],
)
def test_rejects_mutating_or_multiple_statements(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        SqlGuard().inspect(sql)


def test_accepts_select_and_extracts_relations() -> None:
    result = SqlGuard().inspect(
        "SELECT channel, COUNT(*) FROM analytics.orders GROUP BY channel",
        allowed_relations={"analytics.orders"},
    )
    assert result.relations == frozenset({"analytics.orders"})


def test_accepts_cte_without_treating_alias_as_physical_relation() -> None:
    result = SqlGuard().inspect(
        "WITH recent AS (SELECT * FROM analytics.orders) SELECT COUNT(*) FROM recent",
        allowed_relations={"analytics.orders"},
    )
    assert "analytics.orders" in result.relations


def test_rejects_unauthorized_relation() -> None:
    with pytest.raises(SqlGuardError, match="unauthorized relations"):
        SqlGuard().inspect(
            "SELECT * FROM restricted.payroll",
            allowed_relations={"analytics.orders"},
        )
