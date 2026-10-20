"""The official source audit found leading-zero IDs; importing must preserve their identity."""

import sqlite3

import pytest

from generaldataagent.evaluation.stage_six_import import (
    build_identifier_preserving_database,
    classify_column,
)


@pytest.mark.parametrize(
    "values,expected",
    [
        (["00123", "123"], "TEXT"),
        (["9007199254740993", "9007199254740992"], "TEXT"),
        (["123", "456"], "REAL"),
        (["1,000", "2,000"], "REAL"),
        (["1,2", "12"], "TEXT"),
        (["1.5", "", "2.5"], "REAL"),
        (["NaN", "1"], "TEXT"),
        (["1e999", "2"], "TEXT"),
    ],
)
def test_explicit_column_rules(values, expected):
    assert classify_column(values) == expected


def test_identifier_values_survive_and_numeric_division_does_not_change(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("code,large_id,a,b\n00123,9007199254740993,1,2\n00124,9007199254740995,3,4\n")
    target = tmp_path / "data.sqlite"
    build_identifier_preserving_database({"data": source}, target)
    with sqlite3.connect(target) as db:
        rows = db.execute("SELECT code,large_id,a/b FROM data ORDER BY code").fetchall()
    assert rows == [("00123", "9007199254740993", 0.5), ("00124", "9007199254740995", 0.75)]
    with pytest.raises(ValueError):
        build_identifier_preserving_database({"data": source}, target)
