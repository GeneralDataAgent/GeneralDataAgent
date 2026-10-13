"""Deterministic alias matching for certified metric discovery."""

import re
from dataclasses import dataclass

from generaldataagent.catalog.models import MetricDefinition


@dataclass(frozen=True, slots=True)
class MetricMatch:
    metric: MetricDefinition
    score: int
    matched_alias: str


def find_metric_matches(
    metrics: tuple[MetricDefinition, ...],
    question: str,
    *,
    metric_version: str | None = None,
    data_domains: tuple[str, ...] | None = None,
) -> tuple[MetricMatch, ...]:
    normalized_question = normalize_metric_text(question)
    allowed_domains = set(data_domains) if data_domains is not None else None
    matches: list[MetricMatch] = []
    for metric in metrics:
        if metric_version is not None and metric.version != metric_version:
            continue
        if allowed_domains is not None and metric.data_domain not in allowed_domains:
            continue
        best: tuple[int, str] | None = None
        phrases = (metric.name.replace("_", " "), *metric.aliases)
        for phrase in phrases:
            normalized_phrase = normalize_metric_text(phrase)
            score = _phrase_score(normalized_question, normalized_phrase)
            if score > 0 and (best is None or score > best[0]):
                best = (score, phrase)
        if best is not None:
            matches.append(MetricMatch(metric=metric, score=best[0], matched_alias=best[1]))
    return tuple(
        sorted(
            matches,
            key=lambda item: (-item.score, item.metric.name, item.metric.version),
        )
    )


def normalize_metric_text(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _phrase_score(question: str, phrase: str) -> int:
    if not question or not phrase:
        return 0
    if question == phrase:
        return 10_000 + len(phrase)
    if phrase in question:
        return 1_000 + len(phrase)
    return 0
