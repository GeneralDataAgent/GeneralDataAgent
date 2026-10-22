"""Bounded document retrieval that keeps a citable span for every rule context.

A business rule answer is only auditable if the paragraph that states the rule
can be pointed at. This module splits a supplied document into stable, numbered
sections and selects the ones a request needs, using explicit keyword matches
rather than a model. Every selection carries the exact source span, so a
downstream computation can cite it and a reviewer can check it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from generaldataagent.contracts.rules import DocumentPassage, PassageSelection

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MAX_PASSAGE_CHARS = 8_000


class PassageError(ValueError):
    """Raised when a document cannot be split or a request cannot be answered."""


def split_passages(text: str, *, source: str) -> tuple[DocumentPassage, ...]:
    """Split a Markdown-like document into heading-delimited passages.

    Text before the first heading becomes its own passage so nothing is
    silently dropped. Line numbers are one-based and inclusive.
    """
    if not source.strip():
        raise PassageError("source identifier must not be blank")
    lines = text.splitlines()
    passages: list[DocumentPassage] = []
    heading: str | None = None
    buffer: list[str] = []
    start = 1

    def flush(end: int) -> None:
        nonlocal buffer, start
        body = "\n".join(buffer).strip()
        if body:
            if len(body) > _MAX_PASSAGE_CHARS:
                raise PassageError(
                    f"passage at {source}:{start} exceeds {_MAX_PASSAGE_CHARS} characters"
                )
            last_content = len(buffer) - len(list(filter(None, buffer)))
            passages.append(
                DocumentPassage(
                    source=source,
                    heading=heading or "",
                    start_line=start,
                    end_line=max(start, end - last_content),
                    text=body,
                )
            )
        buffer = []

    for number, line in enumerate(lines, start=1):
        match = _HEADING.match(line)
        if match is not None:
            flush(number - 1)
            # A new heading starts a new passage, excluding the heading line
            # itself from the previous body.
            heading = match.group(2).strip()
            start = number + 1
            buffer = []
            continue
        buffer.append(line)
    flush(len(lines))
    return tuple(passages)


def select_passages(
    passages: Sequence[DocumentPassage],
    *,
    required_terms: Iterable[str],
    optional_terms: Iterable[str] = (),
    limit: int = 5,
) -> PassageSelection:
    """Select passages containing required terms, ranked by optional matches.

    A request with no required-term hit returns an empty selection rather than
    the whole document: silently retrieving everything would let a caller
    believe the relevant rule was found.
    """
    required = tuple(term.strip() for term in required_terms if term.strip())
    optional = tuple(term.strip() for term in optional_terms if term.strip())
    if not required:
        raise PassageError("at least one required term is needed to select passages")
    if not 1 <= limit <= 50:
        raise PassageError("limit must be between 1 and 50")

    scored = []
    for passage in passages:
        haystack = passage.text.casefold()
        if not all(term.casefold() in haystack for term in required):
            continue
        score = sum(1 for term in optional if term.casefold() in haystack)
        scored.append((score, passage.start_line, passage))
    if not scored:
        return PassageSelection(selected=(), required_terms=required, hit_count=0)
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = tuple(passage for _, _, passage in scored[:limit])
    return PassageSelection(
        selected=selected,
        required_terms=required,
        hit_count=len(scored),
    )


def citation(passage: DocumentPassage) -> str:
    """Stable citation string a result can carry verbatim."""
    heading = f"#{passage.heading}" if passage.heading else ""
    return f"{passage.source}{heading}:{passage.start_line}-{passage.end_line}"


@dataclass(frozen=True, slots=True)
class RuleCitation:
    """A located rule statement with its exact span."""

    rule_id: str
    passage: DocumentPassage

    @property
    def reference(self) -> str:
        return citation(self.passage)


def resolve_rule_citations(
    passages: Sequence[DocumentPassage], rule_ids: Iterable[str]
) -> tuple[RuleCitation, ...]:
    """Attach each rule id to the first passage that mentions it as a token.

    A rule without any citation is an error, because an un-cited rule would
    make the resulting fee unverifiable.
    """
    resolved: list[RuleCitation] = []
    for rule_id in rule_ids:
        pattern = re.compile(rf"(?<![\w-]){re.escape(rule_id)}(?![\w-])")
        match = next(
            (passage for passage in passages if pattern.search(passage.text)),
            None,
        )
        if match is None:
            raise PassageError(f"rule {rule_id!r} has no citing passage")
        resolved.append(RuleCitation(rule_id=rule_id, passage=match))
    return tuple(resolved)
