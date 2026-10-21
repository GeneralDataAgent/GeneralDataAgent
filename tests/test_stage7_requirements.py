"""Adversarial condition scopes and asymmetric ratios, independent of benchmark labels."""

import pytest
from pydantic import ValidationError

from generaldataagent.contracts.semantic_requirements import SemanticRequirements
from generaldataagent.tools.semantic_requirements import (
    check_requirements,
    validate_requirement_sources,
)


def test_predicate_must_be_required_conjunct_in_the_declared_clause_and_scope():
    req = SemanticRequirements.model_validate(
        {"predicates": [{"expression": "kind='A'", "source_excerpt": "kind A"}]}
    )
    assert not check_requirements("SELECT id FROM t WHERE kind='A' AND id>0", req)
    for sql in [
        "SELECT id FROM t WHERE kind='A' OR id>0",
        "SELECT id FROM t GROUP BY id HAVING kind='A'",
        "WITH x AS(SELECT id FROM t WHERE kind='A') SELECT id FROM t",
    ]:
        assert "predicate_placement_or_population" in check_requirements(sql, req)
    scoped = SemanticRequirements.model_validate(
        {
            "predicates": [
                {"expression": "kind='A'", "source_excerpt": "kind A", "scope": "filtered"}
            ]
        }
    )
    assert not check_requirements(
        "WITH filtered AS(SELECT id FROM t WHERE kind='A') SELECT id FROM filtered", scoped
    )


def test_date_encoding_and_exact_inclusivity_are_checked():
    req = SemanticRequirements.model_validate(
        {
            "dates": [
                {
                    "column": "day",
                    "start": "2026-01-01",
                    "end": "2026-02-01",
                    "source_excerpt": "January",
                }
            ]
        }
    )
    assert not check_requirements(
        "SELECT id FROM t WHERE day>='2026-01-01' AND day<'2026-02-01'", req
    )
    assert "date_boundary_or_encoding" in check_requirements(
        "SELECT id FROM t WHERE day>='2026-01-01' AND day<='2026-02-01'", req
    )
    with pytest.raises(ValidationError):
        SemanticRequirements.model_validate(
            {
                "dates": [
                    {
                        "column": "day",
                        "start": "2026-02-01",
                        "end": "2026-01-01",
                        "source_excerpt": "January",
                    }
                ]
            }
        )


def test_ratio_direction_population_scale_and_zero_guard():
    req = SemanticRequirements.model_validate(
        {
            "ratios": [
                {
                    "output_column": "growth",
                    "numerator": "end_balance-start_balance",
                    "denominator": "start_balance",
                    "multiplier": 100,
                    "source_excerpt": "increase from start to end",
                }
            ]
        }
    )
    for sql in [
        "SELECT (end_balance-start_balance)*100.0/NULLIF(start_balance,0) AS growth FROM t",
        "SELECT 100*(end_balance-start_balance)/NULLIF(start_balance,0) AS growth FROM t",
        "SELECT ((end_balance-start_balance)/NULLIF(start_balance,0))*100 AS growth FROM t",
    ]:
        assert not check_requirements(sql, req)
    errors = check_requirements(
        "SELECT (start_balance-end_balance)/end_balance AS growth FROM t", req
    )
    assert set(errors) == {
        "ratio_numerator",
        "ratio_denominator_population",
        "ratio_scale",
        "ratio_zero_guard",
    }


def test_provenance_is_public_and_does_not_supply_reference_answers():
    req = SemanticRequirements.model_validate(
        {"predicates": [{"expression": "kind='A'", "source_excerpt": "kind A"}]}
    )
    validate_requirement_sources(req, "only kind A", "")
    with pytest.raises(ValueError, match="public source"):
        validate_requirement_sources(req, "different question", "")


def test_public_change_reference_is_resolved_without_model_alias_claims():
    from generaldataagent.tools.semantic_requirements import check_public_change_direction

    question = "What is the percentage change from 2020/1/1 to 2021/1/1?"
    sources = (
        " FROM (SELECT value FROM facts WHERE day='2020-01-01') a, "
        "(SELECT value FROM facts WHERE day='2021-01-01') b"
    )
    assert not check_public_change_direction(
        "SELECT (b.value-a.value)*100/NULLIF(a.value,0)" + sources, question
    )
    errors = check_public_change_direction(
        "SELECT (a.value-b.value)*100/NULLIF(b.value,0)" + sources, question
    )
    assert set(errors) == {"change_direction", "change_reference_period"}
    assert not check_public_change_direction("SELECT x/y FROM facts", question)
    assert not check_public_change_direction(
        "SELECT (a.value-b.value)/b.value" + sources, "ordinary ratio?"
    )


def test_public_rank_and_completed_age_do_not_silently_drop_ties_or_birthdays():
    from generaldataagent.tools.semantic_requirements import check_public_rank_and_age

    question = "List entities among the top 3 in their respective groups."
    assert check_public_rank_and_age(
        "SELECT ROW_NUMBER() OVER(PARTITION BY g ORDER BY score DESC) FROM t", question
    ) == ["rank_ties_require_rank"]
    assert not check_public_rank_and_age(
        "SELECT RANK() OVER(PARTITION BY g ORDER BY score DESC) FROM t", question
    )
    assert not check_public_rank_and_age(
        "SELECT ROW_NUMBER() OVER(ORDER BY score DESC) FROM t",
        "Return exactly top 3; break ties by id",
    )
    year_only = (
        "SELECT CAST(strftime('%Y',arrived) AS INT)-"
        "CAST(strftime('%Y',birthday) AS INT) AS age FROM t"
    )
    assert check_public_rank_and_age(year_only, "What age was the person on arrival?") == [
        "age_requires_birthday_boundary"
    ]
    completed = (
        "SELECT CAST(strftime('%Y',arrived) AS INT)-"
        "CAST(strftime('%Y',birthday) AS INT)-"
        "(strftime('%m-%d',arrived)<strftime('%m-%d',birthday)) AS age FROM t"
    )
    assert not check_public_rank_and_age(completed, "What age was the person on arrival?")


def test_public_tie_policy_cannot_be_omitted_from_an_automatic_contract():
    from generaldataagent.contracts.analysis import AnalysisTaskSpec
    from generaldataagent.tools.sql_semantics import inspect_semantics

    query = "SELECT id FROM people ORDER BY score DESC LIMIT 1"
    for question, instructions in [
        ("Return all people tied for highest score.", ""),
        ("Return people with the highest score.", "Preserve all ties."),
        ("Return people with the highest score.", "Include ties."),
    ]:
        spec = AnalysisTaskSpec(question=question, public_requirements=instructions)
        assert spec.answer.ties == "unspecified"
        assert inspect_semantics(query, spec) == ["ties_may_be_truncated"]
        assert not inspect_semantics(
            "SELECT id FROM people WHERE score=(SELECT MAX(score) FROM people)", spec
        )
    for instructions in ["", "Do not preserve all ties; choose one by id."]:
        assert not inspect_semantics(
            query,
            AnalysisTaskSpec(question="Return one champion.", public_requirements=instructions),
        )


@pytest.mark.parametrize(
    "sql,question,expected",
    [
        ("SELECT id FROM people ORDER BY birthday DESC LIMIT 1", "oldest person", True),
        ("SELECT id FROM people ORDER BY birthday ASC LIMIT 1", "oldest person", False),
        ("SELECT id FROM people ORDER BY birthday DESC LIMIT 1", "youngest person", False),
        ("SELECT id FROM people ORDER BY birthday ASC LIMIT 1", "youngest person", True),
        (
            "SELECT id FROM people p WHERE p.birthday=(SELECT MAX(q.birthday) FROM people q)",
            "oldest people",
            True,
        ),
        (
            "SELECT id FROM people p WHERE p.birthday=(SELECT MIN(q.birthday) FROM people q)",
            "oldest people",
            False,
        ),
        (
            "SELECT id FROM people WHERE (SELECT MIN(birthday) FROM people)=birthday",
            "youngest people",
            True,
        ),
    ],
)
def test_explicit_public_age_encoding_checks_scoped_extrema(sql, question, expected):
    from generaldataagent.tools.semantic_requirements import check_public_age_direction

    instructions = "The larger the birthday value, the younger the person is, and vice versa."
    assert check_public_age_direction(sql, question, instructions) == (
        ["public_age_order_direction"] if expected else []
    )


def test_public_age_direction_does_not_infer_encoding_or_borrow_nested_fields():
    from generaldataagent.tools.semantic_requirements import check_public_age_direction

    policy = "The larger the birthday value, the younger the person is."
    for sql, question, instructions in [
        ("SELECT id FROM people ORDER BY birthday DESC LIMIT 1", "oldest person", ""),
        ("SELECT id FROM people ORDER BY birthday DESC LIMIT 1", "oldest and youngest", policy),
        ("SELECT id FROM people ORDER BY birthday DESC", "oldest person", policy),
        ("SELECT id FROM people ORDER BY -birthday DESC LIMIT 1", "oldest person", policy),
        (
            "SELECT id FROM (SELECT score AS birthday,id FROM people) d "
            "ORDER BY birthday DESC LIMIT 1",
            "oldest person",
            policy,
        ),
        (
            "SELECT p.id FROM people p JOIN other q ON p.id=q.id ORDER BY birthday DESC LIMIT 1",
            "oldest person",
            policy,
        ),
        (
            "SELECT id FROM people WHERE birthday=(SELECT MAX(birthday) FROM other)",
            "oldest person",
            policy,
        ),
        (
            "SELECT id FROM people WHERE id=1 OR birthday=(SELECT MAX(birthday) FROM people)",
            "oldest person",
            policy,
        ),
        (
            "SELECT id FROM people ORDER BY birthday DESC LIMIT 1",
            "oldest person",
            "Do not assume: " + policy,
        ),
        (
            "SELECT id FROM people ORDER BY birthday DESC LIMIT 1",
            "oldest person",
            policy + "\nThe larger the birthday value, the older the person is.",
        ),
    ]:
        assert not check_public_age_direction(sql, question, instructions)
    assert not check_public_age_direction(
        "SELECT id FROM people ORDER BY birthday DESC LIMIT 1",
        "oldest person",
        "The larger the birthday value, the older the person is.",
    )


def test_public_age_direction_rejects_later_birthdays_and_preserves_equal_oldest_dates():
    import sqlite3

    from generaldataagent.contracts.analysis import AnalysisTaskSpec
    from generaldataagent.tools.sql_semantics import inspect_semantics

    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE people(id INTEGER,birthday TEXT);"
        "INSERT INTO people VALUES(1,'1970-01-01'),(2,'1970-01-01'),(3,'2001-12-31');"
    )
    spec = AnalysisTaskSpec(
        question="List IDs of all oldest people.",
        public_requirements="The larger the birthday value, the younger the person is.",
    )
    correct = "SELECT id FROM people WHERE birthday=(SELECT MIN(birthday) FROM people)"
    wrong = correct.replace("MIN", "MAX")
    assert db.execute(correct).fetchall() == [(1,), (2,)]
    assert db.execute(wrong).fetchall() == [(3,)]
    assert not inspect_semantics(correct, spec)
    assert inspect_semantics(wrong, spec) == ["public_age_order_direction"]
    db.close()


def test_period_equalities_in_inner_join_are_resolved_without_losing_population():
    from generaldataagent.tools.semantic_requirements import (
        check_public_change_direction,
        check_public_rank_and_age,
    )

    sql = (
        "SELECT (a.v-b.v)/b.v FROM "
        "(SELECT t.v FROM accounts d JOIN records t ON d.id=t.id AND t.day='2020-01-01') a, "
        "(SELECT t.v FROM accounts d JOIN records t ON d.id=t.id AND t.day='2021-01-01') b"
    )
    assert set(check_public_change_direction(sql, "growth rate from 2020/1/1 to 2021/1/1")) == {
        "change_direction",
        "change_reference_period",
    }
    ranking = (
        "SELECT RANK() OVER(PARTITION BY g ORDER BY score DESC) FROM t WHERE score IS NOT NULL"
    )
    assert check_public_rank_and_age(ranking, "among the top 5 per group") == [
        "unrequested_rank_population_filter"
    ]
    assert not check_public_rank_and_age(
        ranking, "among the top 5 per group; exclude missing scores"
    )


def test_change_direction_resolves_direct_table_aliases_in_their_own_scope():
    from generaldataagent.tools.semantic_requirements import check_public_change_direction

    question = "growth rate from 2020/1/1 to 2021/1/1"
    source = (
        " FROM balances old JOIN balances new ON old.id=new.id "
        "AND old.date='2020-01-01' AND new.date='2021-01-01'"
    )
    assert not check_public_change_direction(
        "SELECT (new.balance-old.balance)*100.0/NULLIF(old.balance,0)" + source, question
    )
    assert set(
        check_public_change_direction(
            "SELECT (old.balance-new.balance)*100.0/new.balance" + source, question
        )
    ) == {"change_direction", "change_reference_period"}
    for ambiguous_source in [
        " FROM balances old LEFT JOIN balances new ON old.id=new.id "
        "AND old.date='2020-01-01' AND new.date='2021-01-01'",
        " FROM balances old JOIN balances new ON old.id=new.id "
        "WHERE (old.date='2020-01-01' AND new.date='2021-01-01') OR old.id=1",
        " FROM balances old JOIN balances new ON old.id=new.id "
        "WHERE EXISTS (SELECT 1 FROM balances old WHERE old.date='2020-01-01')",
    ]:
        assert not check_public_change_direction(
            "SELECT (old.balance-new.balance)*100.0/new.balance" + ambiguous_source, question
        )


def test_change_direction_resolves_scalar_lookups_behind_derived_output_names():
    from generaldataagent.tools.semantic_requirements import check_public_change_direction

    source = (
        " FROM (SELECT "
        "(SELECT v.balance FROM ledger v WHERE v.owner_id=o.id AND v.day='2020-01-01' "
        "ORDER BY v.id LIMIT 1) AS x, "
        "(SELECT v.balance FROM ledger v WHERE v.owner_id=o.id AND v.day='2021-01-01' "
        "ORDER BY v.id LIMIT 1) AS y FROM owners o) amounts"
    )
    question = "What is the growth rate from 2020/1/1 to 2021/1/1?"
    assert not check_public_change_direction(
        "SELECT (amounts.y-amounts.x)*100.0/NULLIF(amounts.x,0)" + source, question
    )
    assert check_public_change_direction(
        "SELECT (amounts.x-amounts.y)*100.0/amounts.y" + source, question
    ) == ["change_direction", "change_reference_period"]
    for ambiguous in [
        source.replace("FROM ledger v", "FROM ledger v JOIN other w ON v.id=w.id").replace(
            "v.balance", "balance"
        ),
        source.replace("v.day='2020-01-01'", "(v.day='2020-01-01' OR v.day='2021-01-01')"),
        source.replace("v.balance", "-v.balance"),
    ]:
        # An independently known denominator may still contradict the reference period,
        # but an unknown numerator cannot establish the subtraction direction.
        assert "change_direction" not in check_public_change_direction(
            "SELECT (amounts.x-amounts.y)*100.0/amounts.y" + ambiguous, question
        )


@pytest.mark.parametrize("cte", [False, True])
def test_growth_periods_follow_unqualified_outputs_of_one_derived_source(cte):
    from generaldataagent.tools.semantic_requirements import check_public_change_direction

    derived = (
        "SELECT (SELECT v.balance FROM ledger v WHERE v.day='2020-01-01') AS x, "
        "(SELECT v.balance FROM ledger v WHERE v.day='2021-01-01') AS y FROM owners o"
    )
    prefix, source = (
        ("WITH amounts AS (" + derived + ") ", " FROM amounts a")
        if cte
        else ("", " FROM (" + derived + ") amounts")
    )
    question = "What is the growth rate from 2020/1/1 to 2021/1/1?"
    correct = prefix + "SELECT (y-x)*100.0/NULLIF(x,0)" + source
    reversed_sql = prefix + "SELECT (x-y)*100.0/y" + source
    assert check_public_change_direction(correct, question) == []
    assert check_public_change_direction(reversed_sql, question) == [
        "change_direction",
        "change_reference_period",
    ]
    assert check_public_change_direction(reversed_sql + " JOIN extras e ON e.id=1", question) == []
    unrelated = "WITH amounts AS (" + derived + ") SELECT (x-y)*100.0/y FROM facts"
    assert check_public_change_direction(unrelated, question) == []


def test_scalar_growth_lookup_can_prove_qualified_periods_through_inner_joins():
    from generaldataagent.tools.semantic_requirements import check_public_change_direction

    sql = (
        "SELECT (b.x-b.y)*100.0/b.y FROM (SELECT "
        "(SELECT v.balance FROM owners o JOIN ledger v ON v.owner_id=o.id "
        "WHERE o.day='2019-06-03' AND v.day='2020-01-01' ORDER BY v.id LIMIT 1) AS x, "
        "(SELECT v.balance FROM owners o JOIN ledger v ON v.owner_id=o.id "
        "WHERE o.day='2019-06-03' AND v.day='2021-01-01' ORDER BY v.id LIMIT 1) AS y) b"
    )
    question = "What is the increase rate from 2020/1/1 to 2021/1/1?"
    assert check_public_change_direction(sql, question) == [
        "change_direction",
        "change_reference_period",
    ]
    assert (
        check_public_change_direction(
            sql.replace("(b.x-b.y)*100.0/b.y", "(b.y-b.x)*100.0/NULLIF(b.x,0)"), question
        )
        == []
    )
    for ambiguous in [
        sql.replace("v.balance", "balance"),
        sql.replace("JOIN ledger", "LEFT JOIN ledger"),
        sql.replace("v.day=", "o.day="),
    ]:
        assert check_public_change_direction(ambiguous, question) == []


def test_public_supplemental_rank_policy_is_checked_with_question():
    from generaldataagent.tools.semantic_requirements import check_public_rank_and_age

    sql = (
        "SELECT ROW_NUMBER() OVER (ORDER BY score DESC, id) AS rn "
        "FROM records WHERE score IS NOT NULL"
    )
    question = "Who is among the top 5 by score?"
    assert set(check_public_rank_and_age(sql, question)) == {
        "rank_ties_require_rank",
        "unrequested_rank_population_filter",
    }
    assert not check_public_rank_and_age(
        sql, question, "Return exactly five. Exclude missing scores."
    )


@pytest.mark.parametrize(
    "formula",
    [
        "year(arrival)-year(birthday)",
        "SUBTRACT(year(`First Date`), year(Birthday))",
        "(year(arrival))-(year(birthday))",
        "SUBTRACT((year(arrival)), year(birthday))",
        "SUBTRACT(year(`Arrival Date`)), year(Birthday))",
        "year(`Arrival Day`)-year(`Birth Day`)",
    ],
)
def test_public_explicit_year_formula_does_not_get_implicit_completed_age_policy(formula):
    from generaldataagent.contracts.analysis import AnalysisTaskSpec, AnswerSpec
    from generaldataagent.tools.sql_semantics import inspect_semantics

    sql = (
        "SELECT CAST(strftime('%Y',arrival) AS INT)-"
        "CAST(strftime('%Y',birthday) AS INT) AS age FROM people"
    )
    spec = AnalysisTaskSpec(
        question="What age was the person on arrival?",
        public_requirements="Age is defined as " + formula,
        answer=AnswerSpec(columns=["age"]),
    )
    assert not inspect_semantics(sql, spec)
    generic = spec.model_copy(
        update={
            "public_requirements": "Use the birthday and arrival year; account for month and day."
        }
    )
    assert inspect_semantics(sql, generic) == ["age_requires_birthday_boundary"]


@pytest.mark.parametrize(
    "correction",
    [
        "minus one when the arrival month/day precedes the birthday.",
        "Account for the month and day.",
        "Use completed years.",
        "- (strftime('%m-%d',arrival)<strftime('%m-%d',birthday))",
    ],
)
def test_explicit_year_formula_does_not_erase_a_public_calendar_correction(correction):
    from generaldataagent.tools.semantic_requirements import check_public_rank_and_age

    policy = "Age = year(arrival)-year(birthday) " + correction
    sql = "SELECT strftime('%Y',arrival)-strftime('%Y',birthday) AS age FROM people"
    assert check_public_rank_and_age(sql, "Age at arrival?", policy) == [
        "age_requires_birthday_boundary"
    ]
    completed = (
        "SELECT strftime('%Y',arrival)-strftime('%Y',birthday)-"
        "(strftime('%m-%d',arrival)<strftime('%m-%d',birthday)) AS age FROM people"
    )
    assert not check_public_rank_and_age(completed, "Age at arrival?", policy)


def test_unrelated_day_output_does_not_replace_an_explicit_year_difference():
    from generaldataagent.tools.semantic_requirements import check_public_rank_and_age

    sql = "SELECT strftime('%Y',arrival)-strftime('%Y',birthday) AS age FROM people"
    policy = "Age = (year(arrival))-(year(birthday)). Also report the day of the lab test."
    assert not check_public_rank_and_age(sql, "Age at arrival?", policy)


@pytest.mark.parametrize("cast_type", ["INTEGER", "REAL"])
def test_casting_a_year_shortcut_does_not_hide_a_missing_birthday_boundary(cast_type):
    from generaldataagent.tools.semantic_requirements import check_public_rank_and_age

    sql = f"SELECT CAST(strftime('%Y',arrival)-strftime('%Y',birthday) AS {cast_type}) FROM people"
    assert check_public_rank_and_age(sql, "Age at arrival?") == ["age_requires_birthday_boundary"]
    assert not check_public_rank_and_age(
        sql, "Age at arrival?", "Use year(arrival)-year(birthday)."
    )


def test_negated_public_examples_do_not_authorize_wrong_rank_or_age_policy():
    from generaldataagent.tools.semantic_requirements import check_public_rank_and_age

    sql = (
        "SELECT ROW_NUMBER() OVER (ORDER BY score DESC) AS rn FROM records WHERE score IS NOT NULL"
    )
    question = "Who is among the top 5 by score?"
    for instructions in [
        "Do not break ties; do not exclude missing scores.",
        "Include missing scores in ranking.",
    ]:
        assert set(check_public_rank_and_age(sql, question, instructions)) == {
            "rank_ties_require_rank",
            "unrequested_rank_population_filter",
        }
    age = (
        "SELECT CAST(strftime('%Y',arrival) AS INT)-"
        "CAST(strftime('%Y',birthday) AS INT) AS age FROM people"
    )
    assert check_public_rank_and_age(
        age, "What age was the person on arrival?", "Do not use year(arrival)-year(birthday)."
    ) == ["age_requires_birthday_boundary"]


@pytest.mark.parametrize(
    ("required", "actual"), [('"Ä"=1', '"ä"=1'), ('"a.b"=1', "a.b=1"), ('"ß"=1', "ss=1")]
)
def test_semantic_predicates_preserve_sqlite_literal_identifier_identity(required, actual):
    req = SemanticRequirements.model_validate(
        {"predicates": [{"expression": required, "source_excerpt": "public field"}]}
    )
    assert check_requirements("SELECT 1 FROM t WHERE " + actual, req) == [
        "predicate_placement_or_population"
    ]
    assert not check_requirements("SELECT 1 FROM t WHERE " + required, req)


def test_semantic_predicates_fold_ascii_and_ignore_identifier_quote_style():
    req = SemanticRequirements.model_validate(
        {"predicates": [{"expression": '"VALUE"=1', "source_excerpt": "public field"}]}
    )
    assert not check_requirements("SELECT id FROM t WHERE value=1", req)


def test_semantic_cte_scopes_do_not_merge_unicode_case_variants():
    req = SemanticRequirements.model_validate(
        {"predicates": [{"expression": "kind=1", "scope": "Ä", "source_excerpt": "public field"}]}
    )
    assert check_requirements(
        'WITH "ä" AS (SELECT id FROM t WHERE kind=1) SELECT * FROM "ä"', req
    ) == ["predicate_scope"]
    assert not check_requirements(
        'WITH "Ä" AS (SELECT id FROM t WHERE kind=1) SELECT * FROM "Ä"', req
    )


def test_public_sum_over_count_all_does_not_allow_implicit_non_null_population():
    from generaldataagent.contracts.analysis import AnalysisTaskSpec
    from generaldataagent.tools.sql_semantics import inspect_semantics

    spec = AnalysisTaskSpec(
        question="Calculate average height for all people.",
        public_requirements="average = DIVIDE(SUM(height), COUNT(all people));",
    )
    sql = "SELECT SUM(height)*1.0/COUNT(*) AS average FROM people WHERE height IS NOT NULL"
    assert inspect_semantics(sql, spec) == ["unrequested_all_count_null_filter"]
    assert not inspect_semantics(sql.replace(" WHERE height IS NOT NULL", ""), spec)
    assert not inspect_semantics(sql.replace("height IS NOT NULL", "other IS NOT NULL"), spec)
    assert not inspect_semantics(
        sql, spec.model_copy(update={"question": "Average height for people with known height?"})
    )
    assert not inspect_semantics(
        sql,
        spec.model_copy(
            update={
                "public_requirements": "average = DIVIDE(SUM(height), COUNT(all known heights));"
            }
        ),
    )
    assert not inspect_semantics(
        sql, spec.model_copy(update={"public_requirements": "Use the usual average."})
    )


def test_public_all_count_does_not_borrow_other_alias_or_nested_scope_filters():
    from generaldataagent.tools.semantic_requirements import check_public_all_count_population

    question, instructions = "average for all people", "DIVIDE(SUM(height), COUNT(all people))"
    sql = (
        "SELECT SUM(p.height)*1.0/COUNT(*) FROM people p JOIN other o ON p.id=o.id "
        "WHERE o.height IS NOT NULL"
    )
    assert not check_public_all_count_population(sql, question, instructions)
    assert check_public_all_count_population(
        sql.replace("o.height IS NOT NULL", "p.height IS NOT NULL"), question, instructions
    ) == ["unrequested_all_count_null_filter"]
    assert not check_public_all_count_population(
        "SELECT SUM(height)*1.0/COUNT(*) FROM people "
        "WHERE EXISTS(SELECT 1 FROM other WHERE height IS NOT NULL)",
        question,
        instructions,
    )
    assert not check_public_all_count_population(
        "SELECT SUM(height)/COUNT(*) FROM people",
        question,
        'DIVIDE(SUM("broken), COUNT(all people))',
    )


def test_physical_root_scope_keeps_predicates_without_borrowing_nested_sources():
    req = SemanticRequirements.model_validate(
        {
            "predicates": [
                {
                    "expression": "score=(SELECT MAX(score) FROM people)",
                    "scope": "people",
                    "source_excerpt": "highest score",
                }
            ]
        }
    )
    assert not check_requirements(
        "SELECT id FROM people WHERE score=(SELECT MAX(score) FROM people)", req
    )
    assert check_requirements(
        "SELECT id FROM people WHERE EXISTS(SELECT 1 FROM other "
        "WHERE score=(SELECT MAX(score) FROM people))",
        req,
    ) == ["predicate_placement_or_population"]
    assert check_requirements(
        "SELECT id FROM other WHERE EXISTS(SELECT 1 FROM people "
        "WHERE score=(SELECT MAX(score) FROM people))",
        req,
    ) == ["predicate_scope"]


def test_root_source_scope_is_not_ambiguous_self_join_or_cte_reference():
    req = SemanticRequirements.model_validate(
        {
            "predicates": [
                {"expression": "p.score=10", "scope": "people", "source_excerpt": "score 10"}
            ]
        }
    )
    assert not check_requirements("SELECT p.id FROM people p WHERE p.score=10", req)
    assert check_requirements(
        "SELECT p.id FROM people p JOIN people q ON p.id=q.id WHERE p.score=10", req
    ) == ["predicate_scope"]
    assert not check_requirements(
        "SELECT p.id FROM people p JOIN people q ON p.id=q.id WHERE p.score=10",
        req.model_copy(
            update={"predicates": [req.predicates[0].model_copy(update={"scope": "p"})]}
        ),
    )
    assert check_requirements(
        "WITH people AS (SELECT id,score FROM base) SELECT p.id FROM people p WHERE p.score=10", req
    ) == ["predicate_placement_or_population"]


def test_physical_scope_source_retains_sqlite_unicode_identifier_identity():
    req = SemanticRequirements.model_validate(
        {"predicates": [{"expression": "kind=1", "scope": "Ä", "source_excerpt": "kind 1"}]}
    )
    assert not check_requirements('SELECT id FROM "Ä" WHERE kind=1', req)
    assert check_requirements('SELECT id FROM "ä" WHERE kind=1', req) == ["predicate_scope"]
