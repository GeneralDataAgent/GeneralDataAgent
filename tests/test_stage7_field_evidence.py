"""Do not turn ambiguous dates, shared names, or unqualified evidence into trusted mappings."""

from generaldataagent.catalog.field_evidence import date_candidates, public_field_mappings


def test_date_candidates_retain_ambiguity_and_do_not_claim_business_validation():
    result = date_candidates("event_date", ["01/02/2026", "02/03/2026"], complete=False)
    assert set(result["candidates"]) == {"month_first_slash", "day_first_slash"}
    assert not result["verified_business_semantics"]
    assert not result["observed_values_complete"]
    assert date_candidates("customer_id", [20260101], complete=True) is None
    assert date_candidates("birth_date", ["2026-02-30"], complete=True)["candidates"] == []
    assert date_candidates("event_date", [20260101], complete=True)["candidates"] == [
        "compact_date"
    ]


def test_only_public_explicit_and_unambiguous_mappings_are_recorded():
    schema = {"schools": ["Virtual", "id"], "events": ["id", "day"]}
    result = public_field_mappings(
        "Exclusively virtual refers to Virtual = 'F'; identity means id", schema
    )
    assert result["mappings"][0]["column"] == "Virtual"
    assert result["mappings"][0]["enum_value"] == "F"
    assert len(result["ambiguous"]) == 1
    assert not public_field_mappings("identity means hidden.id", schema)["mappings"]
    mapped = public_field_mappings("identity means schools.id", schema)
    assert mapped["mappings"][0]["column"] == "id"
