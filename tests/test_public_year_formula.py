"""Prove public year-field sources only when physical ownership is unique."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.tools.public_formulas import check_public_year_fields

SCHEMA = {"people": ["id", "born_at", "admit_at"], "labs": ["person_id", "tested_at"]}
QUESTION = "How old was person 1 upon admission?"
PUBLIC = "Age = SUBTRACT(year(admit_at),year(born_at))."


def query(left="p.admit_at", right="p.born_at"):
    return (
        "SELECT CAST(strftime('%Y',"
        + left
        + ") AS INT)-CAST(strftime('%Y',"
        + right
        + ") AS INT) AS age FROM people p JOIN labs l ON l.person_id=p.id WHERE p.id=1"
    )


@pytest.mark.parametrize(
    "formula",
    [
        PUBLIC,
        "Age = (year(admit_at))-(year(born_at)).",
        "Age = SUBTRACT(year(admit_at)),year(born_at)).",
    ],
)
def test_explicit_formula_uses_original_physical_date_fields(formula):
    assert check_public_year_fields(query(), QUESTION, formula, SCHEMA) == []
    assert check_public_year_fields(query("l.tested_at"), QUESTION, formula, SCHEMA) == [
        "public_year_formula_fields"
    ]
    assert check_public_year_fields(
        query("p.born_at", "p.admit_at"), QUESTION, formula, SCHEMA
    ) == ["public_year_formula_fields"]


def test_unknown_public_mappings_and_derived_lineage_are_not_guessed():
    ambiguous = SCHEMA | {"other": ["born_at"]}
    assert check_public_year_fields(query("l.tested_at"), QUESTION, PUBLIC, ambiguous) == []
    qualified = "Age = year(people.admit_at)-year(people.born_at)."
    assert check_public_year_fields(query("l.tested_at"), QUESTION, qualified, ambiguous) == [
        "public_year_formula_fields"
    ]
    derived = (
        "WITH x AS (SELECT admit_at AS a,born_at AS b FROM people) "
        "SELECT strftime('%Y',x.a)-strftime('%Y',x.b) AS age FROM x"
    )
    assert check_public_year_fields(derived, QUESTION, PUBLIC, SCHEMA) == []
    assert (
        check_public_year_fields(query("l.tested_at"), QUESTION, "Do not use " + PUBLIC, SCHEMA)
        == []
    )
    assert check_public_year_fields(query("l.tested_at"), "ordinary dates?", PUBLIC, SCHEMA) == []
    assert (
        check_public_year_fields(
            query("l.tested_at"),
            QUESTION,
            PUBLIC + " Another = year(admit_at)-year(born_at).",
            SCHEMA,
        )
        == []
    )


def test_unrelated_same_alias_is_not_borrowed_and_other_outputs_are_not_age():
    sql = (
        "WITH ignored AS (SELECT * FROM people p) "
        "SELECT strftime('%Y',p.tested_at)-strftime('%Y',p.tested_at) AS age FROM labs p"
    )
    assert check_public_year_fields(sql, QUESTION, PUBLIC, SCHEMA) == ["public_year_formula_fields"]
    sql = query().replace(
        " AS age FROM", " AS age, year(l.tested_at)-year(p.admit_at) AS stay_years FROM"
    )
    assert check_public_year_fields(sql, QUESTION, PUBLIC, SCHEMA) == []


def test_quoted_physical_fields_in_pseudocode_and_sqlite_unicode_identity():
    schema = {"people": ["First Date", "Birthday", "birthday2"]}
    public = "Age = SUBTRACT(year(`First Date`)),year(Birthday))."
    good = 'SELECT year(p."First Date")-year(p.Birthday) AS age FROM people p'
    assert check_public_year_fields(good, QUESTION, public, schema) == []
    assert check_public_year_fields(
        good.replace("p.Birthday", "p.birthday2"), QUESTION, public, schema
    ) == ["public_year_formula_fields"]
    schema = {"people": ["Åday", "åday", "birth"]}
    sql = 'SELECT year("åday")-year(birth) AS age FROM people'
    assert check_public_year_fields(sql, QUESTION, 'Age = year("Åday")-year(birth).', schema) == [
        "public_year_formula_fields"
    ]


def test_agent_rejects_substituted_event_field_before_execution(tmp_path, monkeypatch):
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    path = tmp_path / "age.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE people(id INT,born_at TEXT,admit_at TEXT);"
            "CREATE TABLE labs(person_id INT,tested_at TEXT);"
            "INSERT INTO people VALUES(1,'1970-12-31','2000-01-01');"
            "INSERT INTO labs VALUES(1,'2005-01-01');"
        )
    bad, good = query("l.tested_at"), query()
    spec = {"answer": {"columns": ["age"], "shape": "scalar"}}
    actions = iter([{"final_sql": bad, "task_spec": spec}, {"final_sql": good, "task_spec": spec}])
    executed = []
    original = SQLiteReadOnlyExecutor.execute

    def observe(self, sql, parameters=()):
        executed.append(sql)
        return original(self, sql, parameters)

    monkeypatch.setattr(SQLiteReadOnlyExecutor, "execute", observe)

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 10}
            )

    before = path.read_bytes()
    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2).run(
        Task("bird-mini-dev-sqlite", "synthetic", QUESTION, PUBLIC, "easy", "age"), path
    )
    assert result["failure"] is None and result["final_sql"] == good
    assert result["trace"][0]["checks"] == ["public_year_formula_fields"]
    assert bad not in executed and good in executed
    assert path.read_bytes() == before
