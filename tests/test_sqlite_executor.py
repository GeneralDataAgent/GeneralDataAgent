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
