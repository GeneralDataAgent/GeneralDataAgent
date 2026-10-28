"""A refused excerpt must be diagnosable without being echoed back."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from generaldataagent.evaluation.stage_nine import excerpt_rejection_diagnostic
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

DOCUMENT = """# Fees

If a field is set to null it means that it applies to all possible values of that field.
fee = fixed_amount + rate * transaction_value / 10000
"""
EXCERPT = DOCUMENT.splitlines()[2]


@pytest.mark.parametrize(
    "excerpt,reason",
    [
        ("", "blank"),
        ("   \n\t", "blank"),
        (f"  {EXCERPT}", "outer_whitespace"),
        (f"{EXCERPT}  ", "outer_whitespace"),
        (f'"{EXCERPT}"', "quoting"),
        (f"“{EXCERPT}”", "quoting"),
        (EXCERPT.replace("null it means", "null means"), "not_verbatim"),
        (EXCERPT.replace("possible values", "possible value"), "not_verbatim"),
        (EXCERPT.replace(" ", "  ", 1), "internal_whitespace"),
        (EXCERPT, "verbatim"),
    ],
)
def test_each_rejection_reason_is_named(excerpt, reason):
    diagnostic = excerpt_rejection_diagnostic(excerpt, DOCUMENT)
    assert diagnostic["reason"] == reason
    assert diagnostic["excerpt_chars"] == len(excerpt)
    assert diagnostic["document_chars"] == len(DOCUMENT)


def test_outer_whitespace_and_quoting_report_where_the_passage_actually_is():
    """The offset is what tells a reviewer this was a near copy, not a guess."""
    for excerpt in (f" {EXCERPT}", f'"{EXCERPT}"'):
        assert excerpt_rejection_diagnostic(excerpt, DOCUMENT)["match_offset"] == (
            DOCUMENT.index(EXCERPT)
        )


def test_a_copy_that_diverges_late_reports_its_verbatim_prefix():
    """A dropped word late in a real passage is not the same failure as a paraphrase."""
    truncated = EXCERPT[:40] + "and it means something else entirely"
    diagnostic = excerpt_rejection_diagnostic(truncated, DOCUMENT)
    assert diagnostic["reason"] == "not_verbatim"
    assert diagnostic["verbatim_prefix_chars"] == 40
    paraphrased = excerpt_rejection_diagnostic("null applies everywhere", DOCUMENT)
    assert paraphrased["verbatim_prefix_chars"] == 0


def test_the_diagnostic_never_returns_the_excerpt_or_the_document():
    """It is a rejection report, not a copy of what the model may retry with."""
    for excerpt in (EXCERPT, f'"{EXCERPT}"', EXCERPT.replace("null", "NULL")):
        rendered = json.dumps(excerpt_rejection_diagnostic(excerpt, DOCUMENT))
        assert EXCERPT not in rendered
        assert "null it means" not in rendered
        assert DOCUMENT not in rendered


def test_diagnostics_require_text():
    with pytest.raises(ValueError):
        excerpt_rejection_diagnostic(None, DOCUMENT)
    with pytest.raises(ValueError):
        excerpt_rejection_diagnostic(EXCERPT, None)


def test_the_refusal_records_why_without_recording_the_excerpt(tmp_path):
    """End to end: the refused step carries the reason and not the rejected text."""
    db = tmp_path / "payments.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE fees (payload TEXT)")
        connection.execute("CREATE TABLE merchant_data (payload TEXT)")
        connection.execute("""CREATE TABLE payments (merchant TEXT, year INT, day_of_year INT,
                   eur_amount REAL, has_fraudulent_dispute TEXT, card_scheme TEXT, aci TEXT,
                   is_credit TEXT, issuing_country TEXT, acquirer_country TEXT)""")

    bad = dict(operation="select", semantics_excerpt=f'"{EXCERPT}"')
    good = dict(operation="select", semantics_excerpt=EXCERPT)

    class Model:
        def __init__(self):
            self.actions = iter([{"rules": bad}, {"rules": good}])

        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(self.actions)),
                tool_calls=[],
                usage_metadata={"total_tokens": 1},
            )

    task = Task("dabstep-dev", "synthetic", "Which rules?", "@answer[list]", "hard", "fees.csv")
    result = BoundedSQLAgent(Model(), max_steps=2, tool_profile="sql-rules").run(
        task, db, documents=DOCUMENT
    )
    refused = [
        event for event in result["trace"] if event.get("code") == "rule_semantics_excerpt_missing"
    ]
    assert len(refused) == 1
    assert refused[0]["excerpt_diagnostic"]["reason"] == "quoting"
    # The rejected excerpt itself is still absent: the reason is enough to retry
    # from the documents, and storing the text would hand it back verbatim. (The
    # accepted excerpt does appear later, in the step that succeeded.)
    assert EXCERPT not in json.dumps(refused[0])
    assert "If a field" not in json.dumps(refused[0])
