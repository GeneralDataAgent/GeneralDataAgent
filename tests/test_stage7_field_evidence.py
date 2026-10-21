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


def test_ambiguous_public_literal_exposes_scoped_counts_without_choosing_meaning(tmp_path):
    import sqlite3

    from generaldataagent.catalog.semantic_context import semantic_context

    path = tmp_path / "ambiguous.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE items(name TEXT); INSERT INTO items VALUES('First');"
            "CREATE TABLE groups(name TEXT); INSERT INTO groups VALUES('Winter');"
            "CREATE TABLE hidden(name TEXT); INSERT INTO hidden VALUES('Winter');"
        )
    before = path.read_bytes()
    result = semantic_context(
        path,
        allowed_relations={"items", "groups"},
        public_text="item group Winter refers to name = 'Winter'",
    )
    record = result["public_field_mappings"]["ambiguous"][0]
    counts = {
        value["candidate"][0]: value["exact_match_rows"] for value in record["literal_observations"]
    }
    assert counts == {"items": 0, "groups": 1}
    assert result["public_literal_checks"] == 2
    assert not result["public_field_mappings"]["mappings"]
    assert "not business certification" in record["observation_scope"]
    assert path.read_bytes() == before


def test_literal_evidence_survives_preview_trimming_and_probe_failures(tmp_path, monkeypatch):
    import json
    import sqlite3

    import generaldataagent.catalog.semantic_context as module
    from generaldataagent.tools.sqlite import QueryExecutionError, SQLiteReadOnlyExecutor

    path = tmp_path / "bounded-literal.sqlite"
    extra = {f"aux_{index}" for index in range(13)}
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE items(name TEXT); CREATE TABLE groups(name TEXT);"
            "INSERT INTO groups VALUES('Winter');"
        )
        for name in sorted(extra):
            db.execute(f'CREATE TABLE "{name}" (id INT)')
    scope = extra | {"items", "groups"}
    text = "item group Winter refers to name = 'Winter'"
    result = module.semantic_context(
        path, allowed_relations=scope, public_text=text, max_chars=1500
    )
    assert result["public_field_mappings"]["ambiguous"][0]["literal_observations"]
    assert len(json.dumps(result)) <= 1500

    class TimeoutExecutor(SQLiteReadOnlyExecutor):
        def execute(self, sql, parameters=()):
            if self.max_rows == 1:
                raise QueryExecutionError("do-not-persist-this-error")
            return super().execute(sql, parameters)

    monkeypatch.setattr(module, "SQLiteReadOnlyExecutor", TimeoutExecutor)
    result = module.semantic_context(
        path, allowed_relations=scope, public_text=text, max_chars=1500
    )
    assert result["public_literal_failures"] == 2
    assert all(
        item["status"] == "unavailable"
        for item in result["public_field_mappings"]["ambiguous"][0]["literal_observations"]
    )
    assert "do-not-persist" not in json.dumps(result)


def test_public_field_identifier_case_and_numeric_literal_probes_are_conservative(tmp_path):
    import sqlite3

    from generaldataagent.catalog.semantic_context import semantic_context

    schema = {"data": ["Σ", "σ", "Mixed"]}
    assert public_field_mappings("signal means Σ", schema)["mappings"][0]["column"] == "Σ"
    assert public_field_mappings("signal means MIXED", schema)["mappings"][0]["column"] == "Mixed"
    assert not public_field_mappings("signal means ω", {"data": ["Ω"]})["mappings"]
    path = tmp_path / "literal-types.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE a(value TEXT); CREATE TABLE b(value TEXT);")
    result = semantic_context(
        path, allowed_relations={"a", "b"}, public_text="threshold means value = 5.00"
    )
    record = result["public_field_mappings"]["ambiguous"][0]
    assert record["literal_kind"] == "number" and result["public_literal_checks"] == 0
    assert "literal_observations" not in record


def test_compact_context_preserves_declared_join_before_empty_preview_metadata(tmp_path):
    import json
    import sqlite3

    from generaldataagent.catalog.semantic_context import semantic_context

    path = tmp_path / "join.sqlite"
    extra = {f"aux_{i}" for i in range(10)}
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE groups(id INT PRIMARY KEY, name TEXT); "
            "CREATE TABLE items(id INT, group_id INT REFERENCES groups(id), name TEXT); "
            "INSERT INTO groups VALUES(1,'Winter'); INSERT INTO items VALUES(2,1,'Snow');"
        )
        for table in sorted(extra):
            db.execute(f'CREATE TABLE "{table}"(id INT)')
    before = path.read_bytes()
    result = semantic_context(
        path,
        allowed_relations=extra | {"items", "groups"},
        max_chars=1500,
        public_text="item group Winter refers to name = 'Winter'",
    )
    assert result["relationships"]["foreign_keys"] == [
        {
            "table": "items",
            "relationship": 0,
            "sequence": 0,
            "column": "group_id",
            "references_table": "groups",
            "references_column": "id",
        }
    ]
    assert result["public_field_mappings"]["ambiguous"][0]["literal_observations"]
    assert len(json.dumps(result)) <= 1500
    assert path.read_bytes() == before
