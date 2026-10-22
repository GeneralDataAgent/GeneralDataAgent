"""Independent numerical checks for finite-population IQR evidence."""

import sqlite3

import numpy as np
import pytest

from generaldataagent.evaluation.stage_six_analysis import fixed_analysis
from generaldataagent.tools.iqr import iqr_summary


@pytest.mark.parametrize(
    "values",
    [
        [4],
        [1, 9],
        [1, 2, 3, 4],
        [0, 0, 0, 2, 2, 2, 5],
        [0, 0, 0, 2, 2, 2, 5.00000001],
        [4] * 9 + [100],
    ],
)
def test_iqr_matches_independent_numpy_quantiles_and_strict_fences(values):
    before = list(values)
    result = iqr_summary(values)
    q1, q3 = np.quantile(values, [0.25, 0.75], method="linear")
    lower, upper = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    assert result["q1"] == pytest.approx(q1)
    assert result["q3"] == pytest.approx(q3)
    assert result["outlier_count"] == sum(x < lower or x > upper for x in values)
    assert values == before


def test_iqr_boundary_and_duplicate_counts_are_preserved():
    assert iqr_summary([0, 0, 0, 2, 2, 2, 5])["outlier_count"] == 0
    assert iqr_summary([0, 0, 0, 2, 2, 2, 5.00000001])["outlier_count"] == 1
    assert iqr_summary([1] * 20 + [100, 100])["outlier_count"] == 2
    assert all(value is None for value in iqr_summary([]).values())


@pytest.mark.parametrize(
    "values", [[float("nan")], [float("inf")], [True], ["3"], [0] * 200001, [-1e308, 1e308]]
)
def test_iqr_refuses_nonfinite_unsupported_and_over_budget_populations(values):
    with pytest.raises(ValueError):
        iqr_summary(values)


def test_describe_iqr_uses_full_columnwise_population_and_keeps_database_unchanged(tmp_path):
    database = tmp_path / "iqr.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE measurements(x REAL, y REAL)")
        db.executemany(
            "INSERT INTO measurements VALUES(?,?)", [(x, None) for x in [0, 0, 0, 2, 2, 2, 6]]
        )
        db.execute("INSERT INTO measurements VALUES(NULL, 5)")
    before = database.read_bytes()
    result = fixed_analysis(
        database,
        {"table": "measurements", "columns": ["x", "y"], "operation": "describe"},
        allowed_relations={"measurements"},
    )
    assert result["iqr_statistics"]["x"]["outlier_count"] == 1
    assert result["iqr_statistics"]["y"]["outlier_count"] == 0
    assert result["statistics"]["x"]["count"] == 7
    assert result["statistics"]["y"]["count"] == 1
    assert "h=(n-1)*p" in result["iqr_method"]
    assert "not publicly verified" in result["iqr_scope"]
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "public",
    [
        "Use nearest quantiles for IQR.",
        "Use exclusive quartiles.",
        "Use 3 times the interquartile range.",
        "Use 2 * IQR fences.",
    ],
)
def test_incompatible_public_convention_cannot_bind_describe_iqr(public):
    from generaldataagent.contracts.analysis import AnswerSpec
    from generaldataagent.tools.iqr import public_iqr_conflicts
    from generaldataagent.tools.sql_semantics import SemanticError, assemble_answers

    conflicts = public_iqr_conflicts(public)
    assert conflicts
    evidence = {
        1: {"iqr_statistics": {"x": {"outlier_count": 1}}, "iqr_public_conflicts": conflicts}
    }
    with pytest.raises(SemanticError, match="iqr_public_method_conflict"):
        assemble_answers(
            {"n": (1, ["iqr_statistics", "x", "outlier_count"])},
            evidence,
            AnswerSpec(columns=["n"]),
        )
    # The same observation may still provide unrelated valid descriptive evidence.
    evidence[1]["statistics"] = {"x": {"count": 7}}
    assert assemble_answers(
        {"n": (1, ["statistics", "x", "count"])}, evidence, AnswerSpec(columns=["n"])
    ) == {"n": 7}


def test_describe_filters_change_population_and_bind_values_as_parameters(tmp_path):
    database = tmp_path / "groups.sqlite"
    label = "O'Brien"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE observations(value REAL, category TEXT)")
        db.executemany(
            "INSERT INTO observations VALUES(?,?)",
            [(x, label) for x in [0, 0, 0, 0, 0, 20]] + [(100, "Other")] * 6,
        )
    before = database.read_bytes()
    args = {"table": "observations", "columns": ["value"], "operation": "describe"}
    all_rows = fixed_analysis(database, args, allowed_relations={"observations"})
    source = "Only category O'Brien."
    filtered = fixed_analysis(
        database,
        args | {"filters": [{"column": "category", "value": label, "source_excerpt": source}]},
        allowed_relations={"observations"},
        public_text=source,
    )
    assert all_rows["iqr_statistics"]["value"]["outlier_count"] == 0
    assert filtered["iqr_statistics"]["value"]["outlier_count"] == 1
    assert filtered["statistics"]["value"]["count"] == 6
    assert filtered["population_filters"] == [
        {"column": "category", "value": label, "operator": "eq"}
    ]
    assert "unverified" in filtered["population_scope"]
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "column,value,excerpt",
    [
        ("absent", "A", "Only A."),
        ("category", "invented", "Only A."),
        ("category", "A", "Forged A."),
    ],
)
def test_describe_rejects_bad_filter_before_scan(tmp_path, monkeypatch, column, value, excerpt):
    from generaldataagent.tools.sqlite import SQLiteReadOnlyExecutor

    database = tmp_path / "scope.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE observations(value REAL, category TEXT)")

    def forbidden(*args, **kwargs):
        raise AssertionError("unverified filter must not scan")

    monkeypatch.setattr(SQLiteReadOnlyExecutor, "execute", forbidden)
    with pytest.raises(ValueError):
        fixed_analysis(
            database,
            {
                "table": "observations",
                "columns": ["value"],
                "operation": "describe",
                "filters": [{"column": column, "value": value, "source_excerpt": excerpt}],
            },
            allowed_relations={"observations"},
            public_text="Only A.",
        )


def test_describe_filter_literal_cannot_escape_parameter_binding(tmp_path):
    database = tmp_path / "parameters.sqlite"
    value = "x' OR 1=1 --"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE observations(value REAL, category TEXT)")
        db.executemany("INSERT INTO observations VALUES(?,?)", [(1, value), (999, "Other")])
    source = "Only label " + value + "."
    result = fixed_analysis(
        database,
        {
            "table": "observations",
            "columns": ["value"],
            "operation": "describe",
            "filters": [{"column": "category", "value": value, "source_excerpt": source}],
        },
        allowed_relations={"observations"},
        public_text=source,
    )
    assert result["statistics"]["value"]["count"] == 1
    assert result["statistics"]["value"]["mean"] == 1


def test_describe_contains_is_literal_case_sensitive_and_parameterized(tmp_path):
    database = tmp_path / "contains.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE observations(value REAL, label TEXT)")
        db.executemany(
            "INSERT INTO observations VALUES(?,?)",
            [(1, "20% share"), (2, "20 percent"), (3, "20% share more"), (4, "20% Share")],
        )
    source = "Use labels containing 20% share."
    args = {
        "table": "observations",
        "columns": ["value"],
        "operation": "describe",
        "filters": [
            {
                "column": "label",
                "value": "20% share",
                "source_excerpt": source,
                "operator": "contains",
            }
        ],
    }
    before = database.read_bytes()
    result = fixed_analysis(database, args, allowed_relations={"observations"}, public_text=source)
    assert result["statistics"]["value"]["count"] == 2
    assert result["statistics"]["value"]["mean"] == 2
    assert result["population_filters"][0]["operator"] == "contains"
    assert database.read_bytes() == before


def test_describe_contains_does_not_expand_independent_test_filter_policy():
    from generaldataagent.contracts.independent_test import IndependentTestSpec

    with pytest.raises(ValueError):
        IndependentTestSpec.model_validate(
            {
                "groups": ["A", "B"],
                "variance": "welch",
                "filters": [
                    {
                        "column": "label",
                        "value": "A",
                        "source_excerpt": "Only A",
                        "operator": "contains",
                    }
                ],
            }
        )
