"""Declared schema relationships preserve scope, composite key order and immutable data."""

import sqlite3

import pytest

from generaldataagent.evaluation.stage_six import digest
from generaldataagent.evaluation.stage_six_sql_precision import schema_relationships


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "schema.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE parent(a INTEGER,b INTEGER,PRIMARY KEY(b,a))")
        db.execute(
            "CREATE TABLE child(x INTEGER,y INTEGER,FOREIGN KEY(x,y) REFERENCES PaReNt(b,a))"
        )
        db.execute('CREATE TABLE "odd""name"(id INTEGER PRIMARY KEY)')
    return path


def test_declared_keys_preserve_composite_order_and_identifier_case(database):
    before = digest(database)
    result = schema_relationships(database, allowed_relations={"parent", "child"})
    assert result["primary_keys"] == {"parent": ["b", "a"]}
    assert [
        (r["column"], r["references_table"], r["references_column"]) for r in result["foreign_keys"]
    ] == [("x", "parent", "b"), ("y", "parent", "a")]
    assert digest(database) == before


def test_schema_context_does_not_expose_relations_outside_scope(database):
    result = schema_relationships(database, allowed_relations={"child"})
    assert result["primary_keys"] == {} and result["foreign_keys"] == []
    with pytest.raises(ValueError, match="unknown schema relation"):
        schema_relationships(database, allowed_relations={"missing"})


def test_quoted_relation_is_safe_and_schema_access_is_read_only(database):
    before = digest(database)
    result = schema_relationships(database, allowed_relations={'odd"name'})
    assert result["primary_keys"] == {'odd"name': ["id"]}
    assert result["foreign_keys"] == [] and digest(database) == before


def test_foreign_key_targets_do_not_merge_distinct_unicode_relations(tmp_path):
    database = tmp_path / "unicode.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript(
            'CREATE TABLE "Ä"(id INT PRIMARY KEY); CREATE TABLE "ä"(id INT PRIMARY KEY); '
            'CREATE TABLE child(parent INT REFERENCES "Ä"(id));'
        )
    before = digest(database)
    result = schema_relationships(database, allowed_relations={"child", "Ä", "ä"})
    assert [row["references_table"] for row in result["foreign_keys"]] == ["Ä"]
    assert not schema_relationships(database, allowed_relations={"child", "ä"})["foreign_keys"]
    assert digest(database) == before
