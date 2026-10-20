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


def test_observed_enum_spelling_candidates_keep_collisions_and_preview_boundaries():
    from generaldataagent.catalog.field_evidence import enum_label_candidates

    text = 'articles published by source "ABC News"'
    result = enum_label_candidates(text, "articles", "source", ["abc-news", "other"], complete=True)
    assert result[0]["observed_values"] == ["abc-news"]
    assert not result[0]["verified_business_semantics"]
    assert not result[0]["ambiguous"]
    collisions = enum_label_candidates(
        text, "articles", "source", ["abc-news", "ABC_News"], complete=True
    )
    assert collisions[0]["ambiguous"]
    assert not enum_label_candidates(text, "articles", "source", ["abc-news"], complete=False)
    assert not enum_label_candidates(text, "articles", "source", ["ABC"], complete=True)


def test_enum_context_is_scoped_complete_and_remains_read_only(tmp_path, monkeypatch):
    import json
    import sqlite3

    import generaldataagent.catalog.semantic_context as context_module
    from generaldataagent.tools.sqlite import QueryExecutionError, SQLiteReadOnlyExecutor

    path = tmp_path / "enum.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE articles(source TEXT); INSERT INTO articles VALUES('abc-news'),('other');"
            "CREATE TABLE hidden(source TEXT); INSERT INTO hidden VALUES('ABC News');"
        )
    before = path.read_bytes()
    text = 'source "ABC News"'
    context = context_module.semantic_context(
        path, allowed_relations={"articles"}, public_text=text
    )
    assert context["observed_value_candidates"][0]["table"] == "articles"
    assert context["observed_value_candidates"][0]["domain_distinct_n"] == 2
    assert len(json.dumps(context)) <= 4000
    assert before == path.read_bytes()

    class TimeoutExecutor(SQLiteReadOnlyExecutor):
        def execute(self, sql, parameters=()):
            if self.max_rows == 65:
                raise QueryExecutionError("fixture timeout")
            return super().execute(sql, parameters)

    monkeypatch.setattr(context_module, "SQLiteReadOnlyExecutor", TimeoutExecutor)
    context = context_module.semantic_context(
        path, allowed_relations={"articles"}, public_text=text
    )
    assert context["enum_domain_failures"] == 1
    assert not context["observed_value_candidates"]
    assert before == path.read_bytes()
