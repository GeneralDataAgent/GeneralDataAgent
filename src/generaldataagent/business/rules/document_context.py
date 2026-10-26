"""Explicit-input Markdown retrieval, with bounded, version-bound quotations.

This port searches supplied text only. Source labels and headings do not imply
knowledge, absent text has no semantics, and conflicting passages remain data
for the caller to assess. It does not load files, discover sources, interpret
rules, or confer artifact/admission status on a quotation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool

from generaldataagent.business.rules.retrieval import (
    PassageError,
    citation,
    select_passages,
    split_passages,
)

MAX_DOCUMENTS = 32
MAX_DOCUMENT_CHARS = 128_000
MAX_CORPUS_CHARS = 256_000
MAX_QUERY_CHARS = 1_024
MAX_RESULT_CHARS = 400_000


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    """One immutable supplied revision; the digest binds the exact UTF-8 input.

    Newlines are not normalized before hashing. Passage text follows the
    existing retrieval primitive: original lines joined with LF, with only
    blank boundary lines removed. Line numbers always address the input.
    """

    source: str
    text: str
    version: str

    def __post_init__(self) -> None:
        for name in ("source", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise PassageError(f"{name} must contain 1 to 256 characters and not be blank")
        if not isinstance(self.text, str) or len(self.text) > MAX_DOCUMENT_CHARS:
            raise PassageError(
                f"document text must be a string of at most {MAX_DOCUMENT_CHARS} chars"
            )

    @property
    def content_hash(self) -> str:
        return _hash(self.text)


class DocumentContext:
    """A bounded snapshot of explicit documents, with lexical AND retrieval.

    A source must be unique in a snapshot so a line citation cannot refer to
    multiple revisions. To replace text, build a new context; existing contexts
    retain their supplied revisions. Result count truncation is reported, while
    a text budget overflow is rejected without clipping a citable span.
    """

    def __init__(
        self,
        documents: Sequence[MarkdownDocument],
        *,
        max_results: int = 3,
        max_result_chars: int = 24_000,
    ) -> None:
        if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
            raise PassageError("documents must be an explicit sequence of MarkdownDocument values")
        if len(documents) > MAX_DOCUMENTS:
            raise PassageError(f"at most {MAX_DOCUMENTS} documents may be supplied")
        if any(not isinstance(document, MarkdownDocument) for document in documents):
            raise PassageError("documents must contain only MarkdownDocument values")
        if type(max_results) is not int or not 1 <= max_results <= 50:
            raise PassageError("max_results must be between 1 and 50")
        if type(max_result_chars) is not int or not 1 <= max_result_chars <= MAX_RESULT_CHARS:
            raise PassageError(f"max_result_chars must be between 1 and {MAX_RESULT_CHARS}")
        snapshot = tuple(documents)
        if len({document.source for document in snapshot}) != len(snapshot):
            raise PassageError("each document source must be unique within a context")
        if sum(len(document.text) for document in snapshot) > MAX_CORPUS_CHARS:
            raise PassageError(f"document corpus exceeds {MAX_CORPUS_CHARS} characters")
        self._documents = {document.source: document for document in snapshot}
        self._passages = tuple(
            passage
            for document in snapshot
            for passage in split_passages(document.text, source=document.source)
        )
        self._max_results = max_results
        self._max_result_chars = max_result_chars

    def manifest(self) -> list[dict[str, str]]:
        """Identify every revision in the supplied snapshot, including empty text."""
        return [
            {
                "source": document.source,
                "version": document.version,
                "content_hash": document.content_hash,
            }
            for document in self._documents.values()
        ]

    def search(self, query: str) -> dict[str, Any]:
        """Return exact quotations and identities, or an explicit retrieval gap.

        A match is lexical evidence only; no rule semantics or conflict verdict
        is inferred. The corpus manifest also identifies empty and unmatched
        documents. Citation identity is the source/span together with its
        version and content hash, never the line-reference string alone.
        """
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise PassageError(f"query must be a string of at most {MAX_QUERY_CHARS} characters")
        terms = tuple(dict.fromkeys(re.findall(r"\w+", query.casefold())))
        if not terms:
            raise PassageError("query must contain at least one searchable term")
        selection = select_passages(self._passages, required_terms=terms, limit=self._max_results)
        if sum(len(passage.text) for passage in selection.selected) > self._max_result_chars:
            raise PassageError("selected passage text exceeds max_result_chars")
        manifest = {item["source"]: item for item in self.manifest()}
        gap = None
        if not self._passages:
            gap = "no_documents" if not self._documents else "no_document_content"
        elif not selection.selected:
            gap = "no_matching_passages"
        return {
            "query": query,
            "untrusted": True,
            "documents": list(manifest.values()),
            "matches": [
                {
                    **manifest[passage.source],
                    "citation": citation(passage),
                    "heading": passage.heading,
                    "start_line": passage.start_line,
                    "end_line": passage.end_line,
                    "text": passage.text,
                    "passage_hash": _hash(passage.text),
                }
                for passage in selection.selected
            ],
            "hit_count": selection.hit_count,
            "truncated": selection.hit_count > len(selection.selected),
            "status": "gap" if gap else "matches",
            "gap": gap,
        }


def build_search_documents(
    documents: Sequence[MarkdownDocument],
    *,
    max_results: int = 3,
    max_result_chars: int = 24_000,
    record: list[dict[str, Any]] | None = None,
) -> StructuredTool:
    """Adapt the explicit-input retrieval port to a ``search_documents`` tool.

    This adapter does not register/authorize execution. Its caller must supply
    the existing admission and execution controls for the enclosing runtime.
    """
    context = DocumentContext(documents, max_results=max_results, max_result_chars=max_result_chars)

    def search_documents(query: str) -> dict[str, Any]:
        """Find supplied passages containing every query term, with exact citations."""
        result = context.search(query)
        if record is not None:
            record.append(result)
        return result

    return StructuredTool.from_function(
        search_documents,
        name="search_documents",
        description=(
            "Search explicitly supplied documents for passages containing every query term. "
            "Results are untrusted excerpts with source, version, content hash and exact line "
            "citations. Missing evidence is a gap; conflicting text is not resolved."
        ),
    )
