"""Synthetic counterexamples independent of benchmark reference answers."""

import sqlite3

import pytest

from generaldataagent.catalog.semantic_context import semantic_context
from generaldataagent.contracts.analysis import AnalysisTaskSpec, AnswerSpec
from generaldataagent.tools.sql_semantics import (
    SemanticError,
    assemble_answers,
    inspect_semantics,
    validate_rows,
)


def test_ties_direction_projection_and_entity_grain():
    spec = AnalysisTaskSpec(
        question="all oldest people",
        order_by="birthday",
        order_direction="asc",
        count_basis="entities",
        answer=AnswerSpec(columns=["id"], ties="all"),
    )
    issues = inspect_semantics("SELECT birthday FROM people ORDER BY birthday DESC LIMIT 1", spec)
    assert set(issues) == {"output_projection", "ties_may_be_truncated", "order_direction"}
    assert not inspect_semantics("SELECT id FROM people ORDER BY birthday ASC", spec)
    assert "joined_entity_count_requires_distinct" in inspect_semantics(
        "SELECT COUNT(*) FROM people JOIN events ON people.id=events.id", spec
    )


def test_denominator_and_post_execution_cardinality():
    spec = AnalysisTaskSpec(question="ratio", denominator="eligible records")
    assert "zero_denominator_unprotected" in inspect_semantics("SELECT 1 / COUNT(*) FROM x", spec)
    assert not inspect_semantics("SELECT 1.0 / NULLIF(COUNT(*), 0) FROM x", spec)
    assert validate_rows(["id"], [], AnswerSpec(empty="reject")) == ["empty_result"]
    assert "duplicate_entities" in validate_rows(["id"], [(1,), (1,)], AnswerSpec(shape="set"))
    assert "incomplete_result" in validate_rows(["id"], [(1,)], AnswerSpec(), complete=False)
    assert "scalar_cardinality" in validate_rows(["x"], [], AnswerSpec(shape="scalar"))


def test_answer_assembly_uses_full_result_and_explicit_rounding_only():
    evidence = {1: {"rows": [(0.252407,)]}}
    bindings = {"r": (1, ["rows", 0, 0])}
    assert assemble_answers(bindings, evidence, AnswerSpec(columns=["r"])) == {"r": 0.252407}
    assert assemble_answers(bindings, evidence, AnswerSpec(columns=["r"], decimals=3)) == {
        "r": "0.252"
    }
    assert evidence[1]["rows"][0][0] == 0.252407
    with pytest.raises(SemanticError):
        assemble_answers({"r": (2, ["rows", 0, 0])}, evidence, AnswerSpec())
    with pytest.raises(SemanticError):
        assemble_answers({"r": (1, ["rows", -1, 0])}, evidence, AnswerSpec())


def test_scoped_context_and_observed_many_to_many(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE p(k INT); CREATE TABLE c(k INT REFERENCES p(k));"
            "CREATE TABLE secret(token TEXT); INSERT INTO p VALUES(1),(1);"
            "INSERT INTO c VALUES(1),(1),(2);"
        )
    before = path.read_bytes()
    result = semantic_context(path, allowed_relations={"p", "c"})
    assert set(result["tables"]) == {"p", "c"}
    assert result["cardinalities"][0]["many_to_many_risk"]
    assert result["tables"]["c"]["row_count"] == 3
    assert before == path.read_bytes()


def test_semantic_context_has_a_global_serialized_budget(tmp_path):
    import json

    path = tmp_path / "wide.sqlite"
    with sqlite3.connect(path) as db:
        columns = ",".join(f"c{i} TEXT" for i in range(50))
        db.execute(f"CREATE TABLE wide ({columns})")
        db.execute(
            "INSERT INTO wide VALUES (" + ",".join("?" for _ in range(50)) + ")",
            ["long sample " * 100] * 50,
        )
    context = semantic_context(path, allowed_relations={"wide"}, max_chars=1500)
    assert len(json.dumps(context)) <= 1500
    assert context["tables"]["wide"]["omitted_fields"] > 0


def test_scope_resolution_rejects_sqlite_double_quoted_string_typos(tmp_path):
    from generaldataagent.tools.sql_semantics import validate_column_scope

    path = tmp_path / "scope.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            'CREATE TABLE people(id INT, "Birth Date" TEXT);'
            "INSERT INTO people VALUES(1,'2000-01-01');"
        )
    before = path.read_bytes()
    for sql in ['SELECT "misspelled" FROM people', "SELECT absent FROM people"]:
        with pytest.raises(SemanticError, match="scoped SQL column"):
            validate_column_scope(path, sql, allowed_relations={"people"})
    for sql in [
        "WITH p AS (SELECT id FROM people) SELECT id FROM p",
        "SELECT id AS identifier FROM people ORDER BY identifier",
        'SELECT "Birth Date" FROM people',
        "SELECT COUNT(*) FROM people",
    ]:
        validate_column_scope(path, sql, allowed_relations={"people"})
    assert path.read_bytes() == before


def test_join_profile_counts_actual_matches_and_excludes_null_or_unmatched_duplicates(tmp_path):
    path = tmp_path / "cardinality.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE p(k INT); CREATE TABLE c(k INT REFERENCES p(k));"
            "INSERT INTO p VALUES(1),(1),(NULL),(NULL),(9),(9);"
            "INSERT INTO c VALUES(1),(1),(2),(2),(NULL),(NULL);"
        )
    result = semantic_context(
        path, allowed_relations={"p", "c"}, public_text="identifier means c.k"
    )
    observed = result["cardinalities"][0]
    assert observed["inner_join_rows"] == 4
    assert observed["matched_left_rows"] == 2
    assert observed["expansion_per_matched_left"] == 2
    assert result["public_field_mappings"]["mappings"][0]["table"] == "c"
