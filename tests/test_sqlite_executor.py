import sqlite3

import pytest

from generaldataagent.tools import QueryExecutionError, SQLiteReadOnlyExecutor


def create_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany("INSERT INTO items(name) VALUES (?)", [("a",), ("b",)])


def test_executes_query_in_read_only_mode(tmp_path) -> None:
    database = tmp_path / "data.db"
    create_database(database)
    result = SQLiteReadOnlyExecutor(database).execute("SELECT name FROM items ORDER BY id")
    assert result.columns == ("name",)
    assert result.rows == (("a",), ("b",))
    assert result.query_plan


def test_database_rejects_writes_even_without_sql_guard(tmp_path) -> None:
    database = tmp_path / "data.db"
    create_database(database)
    with pytest.raises(QueryExecutionError, match="read-only"):
        SQLiteReadOnlyExecutor(database).execute("DELETE FROM items")


def test_enforces_result_row_limit(tmp_path) -> None:
    database = tmp_path / "data.db"
    create_database(database)
    with pytest.raises(QueryExecutionError, match="row limit"):
        SQLiteReadOnlyExecutor(database, max_rows=1).execute("SELECT * FROM items")


def test_optional_cell_budget_accounts_for_width_and_never_returns_prefix(tmp_path):
    database = tmp_path / "cells.db"
    create_database(database)
    before = database.read_bytes()
    result = SQLiteReadOnlyExecutor(database, max_rows=10, max_cells=4).execute(
        "SELECT id,name FROM items"
    )
    assert len(result.rows) == 2 and len(result.columns) == 2
    with pytest.raises(QueryExecutionError, match="cell limit"):
        SQLiteReadOnlyExecutor(database, max_rows=10, max_cells=3).execute(
            "SELECT id,name FROM items"
        )
    with pytest.raises(QueryExecutionError, match="cell limit"):
        SQLiteReadOnlyExecutor(database, max_rows=10, max_cells=1).execute(
            "SELECT id,name FROM items"
        )
    assert database.read_bytes() == before


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_cell_budget_requires_positive_integer(tmp_path, value):
    database = tmp_path / "invalid.db"
    create_database(database)
    with pytest.raises(ValueError, match="positive integer"):
        SQLiteReadOnlyExecutor(database, max_cells=value)
