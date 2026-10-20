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
