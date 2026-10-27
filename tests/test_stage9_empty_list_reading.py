"""The empty-list reading is the caller's to make; the tool must show its effect.

The DABstep annex encodes "applies to every value" as `[]` for most list fields,
and the manual only defines `null`. The tool reports both readings' facet counts
rather than picking one, so the choice stays visible in the result and trace.
"""

import json
import sqlite3

import pytest

from generaldataagent.evaluation.stage_nine import analyze_fee_rules

DOCUMENT = """# Fees

If a field is set to null it means that it applies to all possible values of that field.
fee = fixed_amount + rate * transaction_value / 10000
"""
EXCERPT = DOCUMENT.splitlines()[2]


def annex_rows():
    # Two rules apply to account type R by listing it; two apply to R only via
    # an empty list. A caller reading `[]` as "any value" selects four.
    return [
        dict(ID=1, card_scheme="Orbit", account_type=["R"], aci=["B"], fixed_amount=0.1, rate=10),
        dict(ID=2, card_scheme="Orbit", account_type=["R"], aci=[], fixed_amount=0.2, rate=20),
        dict(ID=3, card_scheme="Orbit", account_type=[], aci=["B"], fixed_amount=0.3, rate=30),
        dict(ID=4, card_scheme="Orbit", account_type=[], aci=[], fixed_amount=0.4, rate=40),
        dict(ID=5, card_scheme="Other", account_type=["R"], aci=["B"], fixed_amount=0.5, rate=50),
    ]


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "annex.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fees (payload TEXT)")
        connection.executemany(
            "INSERT INTO fees VALUES (?)",
            [(json.dumps(row),) for row in annex_rows()],
        )
    return path


def analyze(database, **extra):
    return analyze_fee_rules(
        database,
        dict(
            operation="select",
            semantics_excerpt=EXCERPT,
            card_scheme="Orbit",
            account_type="R",
            aci="B",
            **extra,
        ),
        allowed_relations={"fees"},
        documents=DOCUMENT,
    )


@pytest.mark.parametrize("declared", [False, True])
def test_both_readings_are_counted_under_the_same_facets(database, declared):
    reading = analyze(database, empty_list_is_wildcard=declared)["empty_list_reading"]
    assert reading["declared_empty_list_is_wildcard"] is declared
    # Same request, same facet stage, only the reading differs.
    assert reading["facet_selected_under_declared_reading"] == (4 if declared else 1)
    assert reading["facet_selected_under_alternate_reading"] == (1 if declared else 4)
    assert reading["reading_changes_selection"] is True


def test_a_reading_that_changes_nothing_is_reported_as_such(tmp_path):
    """Annex without empty lists: the flag is inert, and the report says so."""
    path = tmp_path / "listed.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fees (payload TEXT)")
        connection.executemany(
            "INSERT INTO fees VALUES (?)",
            [(json.dumps(row),) for row in annex_rows() if row["account_type"] and row["aci"]],
        )
    reading = analyze(path, empty_list_is_wildcard=False)["empty_list_reading"]
    assert reading["reading_changes_selection"] is False
    assert (
        reading["facet_selected_under_declared_reading"]
        == reading["facet_selected_under_alternate_reading"]
        == 1
    )


def test_the_report_never_changes_the_returned_selection(database):
    """It is an observation: the declared reading still decides the answer."""
    narrow = analyze(database, empty_list_is_wildcard=False)
    wide = analyze(database, empty_list_is_wildcard=True)
    assert narrow["items"] == [1]
    assert wide["items"] == [1, 2, 3, 4]
    for result in (narrow, wide):
        assert (
            result["semantics"]["empty_list_is_wildcard"]
            is result["empty_list_reading"]["declared_empty_list_is_wildcard"]
        )
        assert result["total_count"] == len(result["items"])


def test_the_observation_reaches_the_model_on_a_successful_rule_call(tmp_path):
    """A count the model never sees cannot inform its next call."""
    from types import SimpleNamespace

    from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

    db = tmp_path / "annex.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE fees (payload TEXT)")
        connection.executemany(
            "INSERT INTO fees VALUES (?)", [(json.dumps(row),) for row in annex_rows()]
        )
        connection.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")

    actions = iter(
        [
            {
                "rules": dict(
                    operation="select",
                    semantics_excerpt=EXCERPT,
                    card_scheme="Orbit",
                    account_type="R",
                    aci="B",
                )
            },
            {"answers": {"answer": "1"}, "evidence_steps": [1]},
        ]
    )

    class Model:
        def __init__(self):
            self.messages = []

        def invoke(self, messages):
            self.messages = list(messages)
            return SimpleNamespace(
                content=json.dumps(next(actions)),
                tool_calls=[],
                usage_metadata={"total_tokens": 1},
            )

    task = Task("dabstep-dev", "synthetic", "Which rules?", "@answer[list]", "hard", "fees.csv")
    model = Model()
    BoundedSQLAgent(model, max_steps=2, tool_profile="sql-rules").run(task, db, documents=DOCUMENT)
    # The first human message is the task itself; the step observation follows it.
    observations = [
        json.loads(text)
        for role, text in model.messages
        if role == "human" and "empty_list_reading" in text
    ]
    assert len(observations) == 1
    assert observations[0]["empty_list_reading"]["facet_selected_under_declared_reading"] == 1
    assert observations[0]["empty_list_reading"]["facet_selected_under_alternate_reading"] == 4


def _applicable_database(tmp_path, *, empty_only_rule=True):
    """A merchant whose reachable rules at the final stage depend on the reading.

    No facet is supplied, so the facet stage cannot distinguish the readings:
    both select every rule. Only applicability decides, which is exactly the
    stage a facet-only report could not describe.
    """
    path = tmp_path / "applicable.sqlite"
    annex = [
        dict(
            ID=1,
            card_scheme="Alpha",
            account_type=[],
            capture_delay=None,
            monthly_fraud_level=None,
            monthly_volume=None,
            merchant_category_code=[],
            is_credit=None,
            aci=[],
            intracountry=None,
            fixed_amount=0.1,
            rate=1,
        ),
        dict(
            ID=2,
            card_scheme="Alpha",
            account_type=["R"],
            capture_delay=None,
            monthly_fraud_level=None,
            monthly_volume=None,
            merchant_category_code=[5411],
            is_credit=None,
            aci=["B"],
            intracountry=None,
            fixed_amount=0.2,
            rate=2,
        ),
    ]
    if not empty_only_rule:
        annex = annex[1:]
    merchant = dict(
        merchant="Shop", account_type="R", merchant_category_code=5411, capture_delay="1"
    )
    with sqlite3.connect(path) as db:
        for table, values in [("fees", annex), ("merchant_data", [merchant])]:
            db.execute(f"CREATE TABLE {table} (payload TEXT)")
            db.executemany(f"INSERT INTO {table} VALUES (?)", [(json.dumps(v),) for v in values])
        db.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")
        db.execute(
            "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("Shop", 2023, 1, 100, "False", "Alpha", "B", "True", "NL", "NL"),
        )
    return path


APPLICABLE_REQUEST = dict(
    operation="applicable", semantics_excerpt=EXCERPT, merchant="Shop", year=2023, month=1
)


def _run(path, declared):
    return analyze_fee_rules(
        path,
        APPLICABLE_REQUEST | {"empty_list_is_wildcard": declared},
        allowed_relations={"fees", "merchant_data", "payments"},
        documents=DOCUMENT,
    )


def test_the_reading_is_reported_after_the_stage_that_decides_the_answer(tmp_path):
    """Facet counts can be equal while the returned rules differ completely.

    A real DABstep run reported "reading_changes_selection: false" for an
    `applicable` call whose declared reading returned 0 rules and whose alternate
    reading returned the 34 the published answer requires.
    """
    path = _applicable_database(tmp_path)
    narrow = _run(path, False)
    wide = _run(path, True)
    for result, items in ((narrow, [2]), (wide, [1, 2])):
        reading = result["empty_list_reading"]
        assert reading["selection_stage"] == "applicability"
        # The facet stage is blind here: both readings select both rules.
        assert reading["facet_selected_under_declared_reading"] == 2
        assert reading["facet_selected_under_alternate_reading"] == 2
        assert result["items"] == items
        assert reading["selected_under_declared_reading"] == len(items)
        assert reading["selected_under_alternate_reading"] == 3 - len(items)
        assert reading["reading_changes_selection"] is True


def test_a_reading_that_the_final_stage_absorbs_is_reported_as_no_change(tmp_path):
    """No empty list in the annex: the flag is inert all the way through."""
    path = _applicable_database(tmp_path, empty_only_rule=False)
    reading = _run(path, False)["empty_list_reading"]
    assert reading["selection_stage"] == "applicability"
    assert reading["selected_under_declared_reading"] == 1
    assert reading["selected_under_alternate_reading"] == 1
    assert reading["reading_changes_selection"] is False


def test_an_indeterminate_alternate_reading_is_not_reported_as_no_change(tmp_path):
    """The alternate count can be unknowable; report that, never "no change"."""
    path = _applicable_database(tmp_path)
    # The empty-list rule is excluded under the declared reading when facets are
    # supplied, but included under the alternate one. Make only that rule depend
    # on domesticity, and leave one payment's acquiring country unknown.
    with sqlite3.connect(path) as db:
        (payload,) = db.execute(
            "SELECT payload FROM fees WHERE payload LIKE '%\"ID\": 1%'"
        ).fetchone()
        record = json.loads(payload)
        record["intracountry"] = True
        db.execute("UPDATE fees SET payload = ? WHERE payload = ?", (json.dumps(record), payload))
        # Every transaction must leave domesticity unknown, or a known match
        # settles the rule and the alternate reading stays computable.
        db.execute("UPDATE payments SET acquirer_country = NULL")
    result = analyze_fee_rules(
        path,
        APPLICABLE_REQUEST | {"empty_list_is_wildcard": False, "account_type": "R", "aci": "B"},
        allowed_relations={"fees", "merchant_data", "payments"},
        documents=DOCUMENT,
    )
    reading = result["empty_list_reading"]
    assert result["items"] == [2]
    assert reading["alternate_reading_indeterminate"] is True
    assert reading["selected_under_alternate_reading"] is None
    assert reading["reading_changes_selection"] is None
