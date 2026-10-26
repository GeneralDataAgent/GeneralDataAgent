"""Synthetic explicit-document retrieval checks; no provider or live data calls."""

import hashlib

import pytest

from generaldataagent.business.rules.document_context import (
    MAX_CORPUS_CHARS,
    MAX_DOCUMENT_CHARS,
    MAX_DOCUMENTS,
    MAX_QUERY_CHARS,
    MAX_RESULT_CHARS,
    DocumentContext,
    MarkdownDocument,
    build_search_documents,
)
from generaldataagent.business.rules.retrieval import PassageError


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def document(text, source="manual.md", version="v1"):
    return MarkdownDocument(source=source, text=text, version=version)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiple_sources_keep_original_spans_and_revision_hashes(newline):
    text = newline.join(
        ["  preamble  ", "", "# Country", "", "  issuer equals acquirer  ", "", "keep tabs\t", ""]
    )
    other = "# Another\n\nissuer differs from acquirer\n"
    documents = [document(text), document(other, source="other.md", version="2026-02")]
    result = DocumentContext(documents).search("issuer acquirer")

    assert result["untrusted"] is True
    assert result["status"] == "matches"
    assert result["gap"] is None
    assert result["hit_count"] == 2
    assert result["truncated"] is False
    assert {match["source"] for match in result["matches"]} == {"manual.md", "other.md"}
    for match in result["matches"]:
        original = next(doc for doc in documents if doc.source == match["source"])
        assert match["content_hash"] == digest(original.text)
        assert match["version"] == original.version
        expected = "\n".join(
            original.text.splitlines()[match["start_line"] - 1 : match["end_line"]]
        )
        assert match["text"] == expected
        assert match["passage_hash"] == digest(expected)
    match = next(match for match in result["matches"] if match["source"] == "manual.md")
    assert match["citation"] == "manual.md#Country:5-7"
    assert match["text"] == "  issuer equals acquirer  \n\nkeep tabs\t"
    assert result["documents"] == [
        {"source": doc.source, "version": doc.version, "content_hash": digest(doc.text)}
        for doc in documents
    ]
    preamble = DocumentContext(documents).search("preamble")["matches"][0]
    assert preamble["citation"] == "manual.md:1-1"
    assert preamble["text"] == "  preamble  "


@pytest.mark.parametrize(
    ("documents", "gap"),
    [
        ([], "no_documents"),
        ([document("")], "no_document_content"),
        ([document("# Domestic\n\n")], "no_document_content"),
        ([document("# Domestic\nNo definition is supplied.")], "no_matching_passages"),
    ],
)
def test_missing_evidence_has_an_explicit_gap_without_fallback(documents, gap):
    result = DocumentContext(documents).search("issuer acquirer")
    assert result["status"] == "gap"
    assert result["gap"] == gap
    assert result["matches"] == []
    assert result["hit_count"] == 0
    assert result["truncated"] is False
    assert len(result["documents"]) == len(documents)


def test_source_labels_headings_and_null_text_cannot_supply_unwritten_semantics():
    context = DocumentContext(
        [document("# Empty list domesticity\n\nnull accepts all categories", source="issuer.md")]
    )
    for query in ("issuer", "domesticity", "empty list"):
        assert context.search(query)["gap"] == "no_matching_passages"
    result = context.search("null")
    assert result["matches"][0]["text"] == "null accepts all categories"
    assert "empty_list_is_wildcard" not in result
    assert result["untrusted"] is True


def test_conflicting_sources_are_both_returned_without_choosing_a_rule():
    context = DocumentContext(
        [
            document("# Lists\n\nAn empty list accepts all categories.", source="policy-a.md"),
            document("# Lists\n\nAn empty list accepts no categories.", source="policy-b.md"),
        ]
    )
    result = context.search("empty list")
    assert [match["text"] for match in result["matches"]] == [
        "An empty list accepts all categories.",
        "An empty list accepts no categories.",
    ]
    assert result["hit_count"] == 2
    assert result["truncated"] is False
    assert result["gap"] is None
    assert "conflict" not in result
    assert "empty_list_is_wildcard" not in result


def test_replacing_input_changes_evidence_identity_and_preserves_existing_snapshot():
    supplied = [document("# Lists\nAn empty list accepts all categories.")]
    old = DocumentContext(supplied)
    before = old.search("empty list")
    supplied[0] = document("# Lists\nAn empty list accepts no categories.", version="v2")
    after = DocumentContext(supplied).search("empty list")
    assert old.search("empty list") == before
    assert before["matches"][0]["citation"] == after["matches"][0]["citation"]
    assert before["matches"][0]["content_hash"] != after["matches"][0]["content_hash"]
    assert before["matches"][0]["passage_hash"] != after["matches"][0]["passage_hash"]
    assert after["matches"][0]["version"] == "v2"
    # Even if a caller forgets to bump the declared revision, the content hash
    # distinguishes the text. A version label alone is never evidence identity.
    same_label = document(supplied[0].text)
    assert same_label.content_hash != before["matches"][0]["content_hash"]


def test_hash_binds_entire_document_including_uncited_lines_and_final_newline():
    first = document("# Lists\nempty list\n\n# Other\noriginal\n")
    changed = document("# Lists\nempty list\n\n# Other\nchanged\n")
    a = DocumentContext([first]).search("empty list")["matches"][0]
    b = DocumentContext([changed]).search("empty list")["matches"][0]
    assert a["text"] == b["text"]
    assert a["passage_hash"] == b["passage_hash"]
    assert a["content_hash"] != b["content_hash"]
    assert first.content_hash != document(first.text.rstrip("\n")).content_hash


def test_count_limit_signals_omitted_hits_and_does_not_hide_truncation():
    context = DocumentContext(
        [document("# One\nempty list\n# Two\nempty list\n# Three\nempty list")],
        max_results=2,
    )
    result = context.search("EMPTY empty list")
    assert len(result["matches"]) == 2
    assert result["hit_count"] == 3
    assert result["truncated"] is True
    assert result["matches"][0]["citation"] == "manual.md#One:2-2"


def test_text_budget_rejects_whole_result_instead_of_clipping_or_dropping_evidence():
    docs = [document("# One\nempty list\n# Two\nempty list")]
    assert len(DocumentContext(docs, max_result_chars=20).search("empty")["matches"]) == 2
    with pytest.raises(PassageError, match="selected passage text exceeds"):
        DocumentContext(docs, max_result_chars=19).search("empty")


def test_tool_adapter_uses_the_port_and_records_only_successful_results():
    records = []
    tool = build_search_documents(
        [document("# Lists\nempty list"), document("# Other\nempty list", source="other.md")],
        record=records,
        max_result_chars=20,
    )
    assert tool.name == "search_documents"
    result = tool.invoke({"query": "empty list"})
    assert len(result["matches"]) == 2
    assert records == [result]
    with pytest.raises(PassageError, match="searchable term"):
        tool.invoke({"query": "---"})
    assert records == [result]
    assert build_search_documents([]).invoke({"query": "anything"})["gap"] == "no_documents"


@pytest.mark.parametrize("query", ["", "---", " \n ", "🟦"])
def test_queries_without_search_terms_are_rejected(query):
    with pytest.raises(PassageError, match="searchable term"):
        DocumentContext([]).search(query)


def test_unicode_search_does_not_silently_drop_non_ascii_terms():
    context = DocumentContext([document("# Rules\n空列表未定义\nnull accepts all")])
    assert context.search("空列表")["matches"][0]["text"].startswith("空列表未定义")
    assert context.search("unknown 空列表")["matches"] == []


@pytest.mark.parametrize("query", ["x" * (MAX_QUERY_CHARS + 1), None, 1])
def test_query_size_and_type_are_bounded(query):
    with pytest.raises(PassageError, match="query must be a string"):
        DocumentContext([]).search(query)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_results": 0}, "max_results"),
        ({"max_results": 51}, "max_results"),
        ({"max_results": True}, "max_results"),
        ({"max_results": 1.5}, "max_results"),
        ({"max_result_chars": 0}, "max_result_chars"),
        ({"max_result_chars": MAX_RESULT_CHARS + 1}, "max_result_chars"),
        ({"max_result_chars": True}, "max_result_chars"),
    ],
)
def test_invalid_result_bounds_are_rejected(kwargs, error):
    with pytest.raises(PassageError, match=error):
        build_search_documents([], **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": ""},
        {"source": " "},
        {"source": "x" * 257},
        {"source": None},
        {"version": ""},
        {"version": " "},
        {"version": "x" * 257},
        {"version": None},
        {"text": None},
        {"text": "x" * (MAX_DOCUMENT_CHARS + 1)},
    ],
)
def test_document_inputs_are_explicit_and_bounded(kwargs):
    values = {"source": "manual.md", "text": "text", "version": "v1", **kwargs}
    with pytest.raises(PassageError):
        MarkdownDocument(**values)


@pytest.mark.parametrize("documents", ["some text", None, ["some text"], {"manual": "text"}])
def test_bare_or_implicit_document_inputs_are_rejected(documents):
    with pytest.raises(PassageError, match="documents must"):
        DocumentContext(documents)


def test_document_count_corpus_size_duplicate_sources_and_passage_size_are_bounded():
    with pytest.raises(PassageError, match="at most"):
        DocumentContext([document("text", source=f"{n}.md") for n in range(MAX_DOCUMENTS + 1)])
    with pytest.raises(PassageError, match="corpus exceeds"):
        DocumentContext([document("x" * MAX_DOCUMENT_CHARS, source=f"{n}.md") for n in range(3)])
    assert 3 * MAX_DOCUMENT_CHARS > MAX_CORPUS_CHARS
    with pytest.raises(PassageError, match="source must be unique"):
        DocumentContext([document("one"), document("two", version="v2")])
    with pytest.raises(PassageError, match="exceeds 8000"):
        DocumentContext([document("# Heading\n" + "x" * 8001)])


def test_exact_maximum_document_and_corpus_size_are_accepted_with_small_passages():
    # Each section is short, while the whole document reaches its exact limit.
    text = ("# Section\n" + "x" * 990 + "\n") * 127
    text += "# Last\n" + "x" * (MAX_DOCUMENT_CHARS - len(text) - len("# Last\n"))
    assert len(text) == MAX_DOCUMENT_CHARS
    result = DocumentContext([document(text), document(text, source="second.md")]).search("missing")
    assert result["gap"] == "no_matching_passages"
