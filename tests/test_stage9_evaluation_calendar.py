import json
import sqlite3

import pytest

from generaldataagent.evaluation.stage_nine import RuleToolError, analyze_fee_rules


@pytest.mark.parametrize("bad_day", [1.5, float("inf"), "1.5", " 1 "])
def test_direct_rule_evaluation_refuses_noncanonical_source_day(tmp_path, bad_day):
    database = tmp_path / "payments.sqlite"
    annex = [dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")]
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
                "INSERT INTO " + table + " VALUES (?)", [(json.dumps(row),) for row in records]
            )
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year TEXT,
            eur_amount TEXT, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
            is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.executemany(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("Shop", 2024, 1, "100", "False", "Alpha", "B", "True", "NL", "NL"),
                ("Shop", 2024, bad_day, "100", "False", "Alpha", "B", "True", "NL", "NL"),
            ],
        )
    with pytest.raises(RuleToolError) as error:
        analyze_fee_rules(
            database,
            dict(
                operation="applicable",
                semantics_excerpt="null means all",
                merchant="Shop",
                year=2024,
                month=1,
            ),
            allowed_relations={"fees", "merchant_data", "payments"},
            documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
        )
    assert error.value.code == "payments_invalid_row"
    assert " 1 " not in error.value.hint


def test_direct_rule_evaluation_accepts_canonical_day_and_digits(tmp_path):
    database = tmp_path / "payments.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.execute(
            "INSERT INTO fees VALUES (?)",
            (json.dumps(dict(ID="A", card_scheme="Alpha", fixed_amount="0.05", rate="14")),),
        )
        db.execute("CREATE TABLE merchant_data (payload TEXT)")
        db.execute(
            "INSERT INTO merchant_data VALUES (?)",
            (json.dumps(dict(merchant="Shop", account_type="R", merchant_category_code=5411)),),
        )
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year TEXT,
            eur_amount TEXT, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
            is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2024, "060", "100", "False", "Alpha", "B", "True", "NL", "NL"),
        )
    result = analyze_fee_rules(
        database,
        dict(
            operation="applicable",
            semantics_excerpt="null means all",
            merchant="Shop",
            year=2024,
            month=2,
        ),
        allowed_relations={"fees", "merchant_data", "payments"},
        documents="null means all\nfee = fixed_amount + rate * transaction_value / 10000",
    )
    assert result["context"]["month"] == 2
    assert result["items"] == ["A"]
