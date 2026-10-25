"""Candidate naming is untrusted; only bounded scoped read-only facts are observed."""

import sqlite3

from generaldataagent.catalog.join_evidence import observed_join_candidates
from generaldataagent.catalog.schema_relationships import schema_relationships


def probe(path, schema, *, question="items", target="groups"):
    return observed_join_candidates(
        path,
        schema=schema,
        relationships=schema_relationships(path, allowed_relations=set(schema)),
        mappings={
            "ambiguous": [
                {
                    "literal_observations": [
                        {"candidate": [target, "name"], "exact_match_rows": 1},
                    ]
                }
            ]
        },
        public_text=question,
    )


def test_join_candidate_counts_nulls_unmatched_keys_and_immutable_data(tmp_path):
    path = tmp_path / "joins.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE groups(id INT PRIMARY KEY, name TEXT);"
            "CREATE TABLE items(group_id INT); CREATE TABLE hidden(group_id INT);"
            "INSERT INTO groups VALUES(1,'Winter');"
            "INSERT INTO items VALUES(1),(1),(2),(NULL);"
        )
    before = path.read_bytes()
    result = probe(path, {"groups": ["id", "name"], "items": ["group_id"]})
    assert result == {
        "candidates": [
            {
                "keys": ["items", "group_id", "groups", "id"],
                "non_null_child_rows": 3,
                "matched_child_rows": 2,
                "matched_join_rows": 2,
                "join_expands_matches": False,
            }
        ],
        "failures": 0,
        "omitted": 0,
    }
    assert path.read_bytes() == before
    assert not probe(path, {"items": ["group_id"]})["candidates"]


def test_candidate_checks_are_bounded_and_prioritize_public_child_relation(tmp_path):
    path = tmp_path / "bounded.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE groups(id INT PRIMARY KEY, name TEXT)")
        for name in ["a", "b", "items", "other"]:
            db.execute(f'CREATE TABLE "{name}"(group_id INT)')
    schema = {name: ["group_id"] for name in ["a", "b", "items", "other"]}
    schema["groups"] = ["id", "name"]
    result = probe(path, schema)
    assert len(result["candidates"]) == 2 and result["omitted"] == 2
    assert result["candidates"][0]["keys"][0] == "items"
    assert all(row["matched_child_rows"] == 0 for row in result["candidates"])


def test_declared_links_and_undeclared_or_composite_parent_keys_are_not_inferred(tmp_path):
    path = tmp_path / "declared.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE groups(id INT PRIMARY KEY, name TEXT);"
            "CREATE TABLE items(group_id INT REFERENCES groups(id));"
            "CREATE TABLE no_pk(id INT,name TEXT); CREATE TABLE child(no_pk_id INT);"
            "CREATE TABLE composite(a INT,b INT,PRIMARY KEY(a,b));"
            "CREATE TABLE other(composite_a INT);"
        )
    assert not probe(path, {"groups": ["id", "name"], "items": ["group_id"]})["candidates"]
    assert not probe(path, {"no_pk": ["id", "name"], "child": ["no_pk_id"]}, target="no_pk")[
        "candidates"
    ]
    assert not probe(path, {"composite": ["a", "b"], "other": ["composite_a"]}, target="composite")[
        "candidates"
    ]


def test_optional_candidate_timeout_is_recorded_without_raw_exception(tmp_path, monkeypatch):
    import generaldataagent.catalog.join_evidence as module
    from generaldataagent.tools.sqlite import QueryExecutionError, SQLiteReadOnlyExecutor

    path = tmp_path / "timeout.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE groups(id INT PRIMARY KEY); CREATE TABLE items(group_id INT);"
        )

    class Timeout(SQLiteReadOnlyExecutor):
        def execute(self, sql, parameters=()):
            assert self.max_rows == 1 and self.timeout_seconds == 1
            raise QueryExecutionError("private exception text")

    monkeypatch.setattr(module, "SQLiteReadOnlyExecutor", Timeout)
    result = probe(path, {"groups": ["id"], "items": ["group_id"]})
    assert result["failures"] == 1
    assert result["candidates"][0]["status"] == "unavailable"
    assert "private exception text" not in str(result)


def test_parent_key_uniqueness_does_not_hide_child_collation_join_expansion(tmp_path):
    path = tmp_path / "collation.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE groups(id TEXT PRIMARY KEY COLLATE BINARY);"
            "CREATE TABLE items(group_id TEXT COLLATE NOCASE);"
            "INSERT INTO groups VALUES('A'),('a'); INSERT INTO items VALUES('A');"
        )
    record = probe(path, {"groups": ["id"], "items": ["group_id"]})["candidates"][0]
    assert record["non_null_child_rows"] == record["matched_child_rows"] == 1
    assert record["matched_join_rows"] == 2 and record["join_expands_matches"]


def test_scalar_declared_fk_target_can_supply_non_primary_candidate_without_uniqueness_claim(
    tmp_path,
):
    path = tmp_path / "target.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE groups(id INT PRIMARY KEY,code TEXT,name TEXT); "
            "CREATE TABLE known(group_code TEXT REFERENCES groups(code)); "
            "CREATE TABLE items(group_code TEXT); "
            "INSERT INTO groups VALUES(1,'A','Winter'),(2,'A','Winter'); "
            "INSERT INTO items VALUES('A');"
        )
    schema = {"groups": ["id", "code", "name"], "known": ["group_code"], "items": ["group_code"]}
    record = probe(path, schema)["candidates"][0]
    assert record["keys"] == ["items", "group_code", "groups", "code"]
    assert record["matched_child_rows"] == 1 and record["matched_join_rows"] == 2
    assert record["join_expands_matches"]
