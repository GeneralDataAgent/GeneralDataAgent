import json
import sqlite3

import pytest

from generaldataagent.evaluation.stage_nine import RuleToolError, analyze_fee_rules


@pytest.mark.parametrize("operation", ["applicable", "counterfactual"])
@pytest.mark.parametrize(
    ("issuing_country", "acquirer_country"),
    [(None, None), (None, "NL"), ("NL", None)],
)
def test_direct_rule_evaluation_refuses_unknown_transaction_domesticity(
    tmp_path, issuing_country, acquirer_country, operation
):
    database = tmp_path / "payments.sqlite"
    annex = [
        dict(
            ID="A",
            card_scheme="Alpha",
            intracountry=True,
            fixed_amount="0.05",
            rate="14",
        )
    ]
    with sqlite3.connect(database) as db:
        for table, records in [
            ("fees", annex),
            (
                "merchant_data",
                [dict(merchant="Shop", account_type="R", merchant_category_code=5411)],
            ),
        ]:
            db.execute(f"CREATE TABLE {table} (payload TEXT)")
            db.executemany(
                f"INSERT INTO {table} VALUES (?)", [(json.dumps(row),) for row in records]
            )
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
            eur_amount TEXT, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
            is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "Shop",
                2024,
                1,
                "100",
                "False",
                "Alpha",
                "B",
                "True",
                issuing_country,
                acquirer_country,
            ),
        )

    with pytest.raises(RuleToolError) as error:
        analyze_fee_rules(
            database,
            dict(
                operation=operation,
                semantics_excerpt="null means all",
                merchant="Shop",
                year=2024,
                month=1,
                **(
                    {"rule_id": "A", "alternate_rate": "20"}
                    if operation == "counterfactual"
                    else {}
                ),
            ),
            allowed_relations={"fees", "merchant_data", "payments"},
            documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
        )
    assert error.value.code == "rule_transaction_facets_indeterminate"
    for country in (issuing_country, acquirer_country):
        if country is not None:
            assert country not in error.value.hint
