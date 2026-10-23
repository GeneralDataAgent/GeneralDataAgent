"""Deterministic translation from a public question to a rule request.

The rule workflow takes one exact ``RuleAnalysisRequest``. Turning a public
question into that request is a parsing problem, and it is done here, outside
the model loop, because every field is a commitment: a wrong merchant, month or
rate change silently answers a different question.

The parser is deliberately narrow. It recognises the shapes it can justify from
the public text and **refuses everything else** with a stable code. A refusal is
a recorded outcome, not a fallback: there is no "guess and let the model fix it"
path, because the model is exactly the party that must not choose these values.

It never reads a label, an expected answer or a reference query.
"""

from __future__ import annotations

import calendar
import re

from generaldataagent.contracts.rule_tools import RuleAnalysisRequest

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


# The semantics excerpt must be copied from the supplied documents; the parser
# cannot invent one. Callers supply it from the same public text they used to
# build the request.
class RuleQuestionError(ValueError):
    """Stable refusal code; never echoes the question text."""

    CODES = frozenset(
        {
            "rule_question_unrecognised_operation",
            "rule_question_missing_merchant",
            "rule_question_missing_time_scope",
            "rule_question_ambiguous_time_scope",
            "rule_question_invalid_time_scope",
            "rule_question_missing_rule_id",
            "rule_question_missing_alternate_rate",
            "rule_question_invalid_rule_id",
            "rule_question_invalid_alternate_rate",
            "rule_question_missing_semantics_excerpt",
            "rule_question_invalid_transaction_value",
            "rule_question_invalid_facet",
            "rule_question_unresolved_mcc",
            "rule_question_unsupported_context_facets",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unknown rule question failure code")
        self.code = code
        super().__init__(code)


def _ordinal_day(text: str, year: int | None) -> int | None:
    """Read exactly one supported day-of-year declaration, never a numeric prefix."""
    pattern = (
        r"\bday\s+(?P<day>[^\s?]+)"
        r"|(?<![\w.-])(?P<year_day>[0-9]+)(?:st|nd|rd|th)?\s+"
        r"(?:of\s+)?(?:the\s+)?year\b"
        r"|(?<![\w.-])(?P<ordinal>[0-9]+)(?:st|nd|rd|th)\b"
    )
    matches = list(re.finditer(pattern, text, re.IGNORECASE))
    if len(matches) > 1:
        raise RuleQuestionError("rule_question_ambiguous_time_scope")
    if not matches:
        if re.search(r"\bday\b", text, re.IGNORECASE):
            raise RuleQuestionError("rule_question_invalid_time_scope")
        return None
    match = matches[0]
    token = next(value for value in match.groupdict().values() if value is not None)
    if token.endswith((".", "!")):
        token = token[:-1]
    maximum = 366 if year is None or calendar.isleap(year) else 365
    if re.fullmatch(r"[0-9]{1,3}", token) is None or not 1 <= int(token) <= maximum:
        raise RuleQuestionError("rule_question_invalid_time_scope")
    return int(token)


def _year(text: str) -> int | None:
    match = re.search(r"\b(19|20|21)\d{2}\b", text)
    return int(match.group(0)) if match else None


def _merchant(question: str, known: tuple[str, ...]) -> str | None:
    lowered = question.casefold()
    found = [name for name in known if name.casefold() in lowered]
    if len(found) != 1:
        return None
    return found[0]


def _numeric_declaration(
    text: str, marker: str, number: str, *, limit: int, error_code: str
) -> str | None:
    declarations = list(re.finditer(marker, text, re.IGNORECASE))
    if not declarations:
        return None
    if len(declarations) != 1:
        raise RuleQuestionError(error_code)
    # Validate the entire whitespace-delimited token. Strip only terminal prose
    # punctuation; units, exponents, lists and fractions must never become prefixes.
    tail = text[declarations[0].end() :].lstrip()
    token = tail.split(maxsplit=1)[0] if tail else ""
    if token.endswith(("?", ".", "!")):
        token = token[:-1]
    if len(token) > limit or re.fullmatch(number, token) is None:
        raise RuleQuestionError(error_code)
    return token


def _rate(text: str) -> str | None:
    return _numeric_declaration(
        text,
        r"\b(?:changed|changes|change)\s+to\b",
        r"[0-9]+(?:\.[0-9]+)?",
        limit=40,
        error_code="rule_question_invalid_alternate_rate",
    )


def _rule_id(text: str) -> str | None:
    return _numeric_declaration(
        text,
        r"\bid\s*=",
        r"[0-9]+",
        limit=128,
        error_code="rule_question_invalid_rule_id",
    )


def parse_rule_question(
    question: str,
    *,
    known_merchants: tuple[str, ...],
    semantics_excerpt: str,
    mcc_descriptions: dict[str, int] | None = None,
    empty_list_is_wildcard: bool = False,
) -> RuleAnalysisRequest:
    """Build the one request the public question justifies, or refuse.

    ``known_merchants`` and ``mcc_descriptions`` come from the public tables,
    not from labels. ``semantics_excerpt`` must be copied from the supplied
    documents by the caller; this function never fabricates one.

    ``empty_list_is_wildcard`` is the caller's explicit reading of the annex.
    It defaults to the fail-closed value, and a caller that sets it takes
    responsibility for the evidence — see
    ``docs/validation/stage10-rule-semantics.md`` for why this is not inferred
    from the data.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("rule question parsing requires the public question text")
    if not semantics_excerpt or not semantics_excerpt.strip():
        raise RuleQuestionError("rule_question_missing_semantics_excerpt")
    if type(empty_list_is_wildcard) is not bool:
        raise ValueError("empty_list_is_wildcard must be a boolean declaration")
    text = question
    lowered = text.casefold()
    merchant = _merchant(text, known_merchants)
    year = _year(text)
    months = [value for name, value in _MONTHS.items() if re.search(rf"\b{name}\b", lowered)]
    if len(months) > 1 or len(set(re.findall(r"\b(?:19|20|21)\d{2}\b", text))) > 1:
        raise RuleQuestionError("rule_question_ambiguous_time_scope")
    month = months[0] if months else None
    if month is not None and re.search(
        rf"\b(?:{'|'.join(_MONTHS)})\s+[0-9]{{1,2}}(?:\b|st\b|nd\b|rd\b|th\b)",
        text,
        re.IGNORECASE,
    ):
        raise RuleQuestionError("rule_question_invalid_time_scope")
    day = _ordinal_day(text, year)
    applies = re.search(r"\bappl(?:y|ies|icable)\b", lowered) is not None
    rate_change = (
        "delta" in lowered and re.search(r"\b(?:changed|changes|change)\b", lowered) is not None
    )
    # A question that declares facet values ("account_type = R") is a pure facet
    # query even when it says "apply"; it needs no merchant context.
    facet_question = ("fee id" in lowered or "fee ids" in lowered) and re.search(
        r"\b(?:account_type|account type|aci)\s*[=:]", lowered
    ) is not None
    if not facet_question and (applies or rate_change) and merchant is None:
        raise RuleQuestionError("rule_question_missing_merchant")

    if (
        merchant is not None
        and (applies or rate_change)
        and re.search(
            r"\b(?:account_type|account type|aci|card scheme|mcc description|transaction value|"
            r"is_credit|intracountry|credit transactions?|debit transactions?)\b",
            lowered,
        )
    ):
        # Context execution uses observed merchant/payment records. Until explicit
        # transaction-subset semantics exist, never drop a stated filter here.
        raise RuleQuestionError("rule_question_unsupported_context_facets")

    if rate_change:
        rule_id = _rule_id(text)
        alternate = _rate(text)
        if rule_id is None:
            raise RuleQuestionError("rule_question_missing_rule_id")
        if alternate is None:
            raise RuleQuestionError("rule_question_missing_alternate_rate")
        if year is None:
            raise RuleQuestionError("rule_question_missing_time_scope")
        return RuleAnalysisRequest(
            operation="counterfactual",
            semantics_excerpt=semantics_excerpt,
            empty_list_is_wildcard=empty_list_is_wildcard,
            merchant=merchant,
            year=year,
            rule_id=rule_id,
            alternate_rate=alternate,
            **_scope(month, day),
        )

    # Applicability is checked before the facet fallback: "the Fee IDs applicable
    # to X in March" is a merchant/time question that mentions fee IDs.
    if applies and merchant is not None:
        if year is None:
            raise RuleQuestionError("rule_question_missing_time_scope")
        return RuleAnalysisRequest(
            operation="applicable",
            semantics_excerpt=semantics_excerpt,
            empty_list_is_wildcard=empty_list_is_wildcard,
            merchant=merchant,
            year=year,
            **_scope(month, day),
        )

    # A pure facet question asks which rules match declared values; it needs no
    # merchant context.
    if "fee id" in lowered or "fee ids" in lowered:
        return RuleAnalysisRequest(
            operation="select",
            semantics_excerpt=semantics_excerpt,
            empty_list_is_wildcard=empty_list_is_wildcard,
            **_facet_fields(text, mcc_descriptions),
        )

    if "average fee" in lowered:
        return RuleAnalysisRequest(
            operation="average_fee",
            semantics_excerpt=semantics_excerpt,
            empty_list_is_wildcard=empty_list_is_wildcard,
            **_facet_fields(text, mcc_descriptions),
        )

    if "applicable" in lowered or "apply" in lowered:
        raise RuleQuestionError("rule_question_missing_merchant")

    raise RuleQuestionError("rule_question_unrecognised_operation")


def _scope(month: int | None, day: int | None) -> dict[str, int]:
    if month is not None and day is not None:
        raise RuleQuestionError("rule_question_ambiguous_time_scope")
    if month is not None:
        return {"month": month}
    if day is not None:
        return {"day_of_year": day}
    raise RuleQuestionError("rule_question_missing_time_scope")


_ACCOUNT_TYPES = ("R", "H", "F")


def _value_for(text: str, markers: tuple[str, ...], *, limit: int) -> str | None:
    """The value after ``marker = value``, ``marker: value`` or ``marker value``."""
    marker = "|".join(re.escape(value) for value in markers)
    declarations = list(re.finditer(rf"\b(?:{marker})\b", text, re.IGNORECASE))
    if not declarations:
        return None
    matches = list(
        re.finditer(
            rf"\b(?:{marker})\b\s*(?:=|:)?\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)",
            text,
            re.IGNORECASE,
        )
    )
    if len(declarations) != 1 or len(matches) != 1 or len(matches[0].group(1)) > limit:
        raise RuleQuestionError("rule_question_invalid_facet")
    return matches[0].group(1)


def _mcc_code(text: str, descriptions: dict[str, int] | None) -> int | None:
    """Resolve a quoted MCC description through the public MCC table."""
    declarations = re.findall(r"\bmcc description\b", text, re.IGNORECASE)
    if not declarations:
        return None
    matches = re.findall(r"\bmcc description\s*:\s*([^,.;?]+)", text, re.IGNORECASE)
    if len(declarations) != 1 or len(matches) != 1 or not descriptions:
        raise RuleQuestionError("rule_question_unresolved_mcc")
    description = matches[0].strip().casefold()
    codes = {code for name, code in descriptions.items() if name.strip().casefold() == description}
    if len(codes) != 1:
        raise RuleQuestionError("rule_question_unresolved_mcc")
    return codes.pop()


def _facet_fields(text: str, descriptions: dict[str, int] | None) -> dict[str, object]:
    """Facets the question names explicitly; everything else stays unconstrained.

    Only exact textual equalities are accepted. Unstated facets stay unconstrained;
    stated but malformed, ambiguous or unresolved facets are refused.
    """
    lowered = text.casefold()
    facets: dict[str, object] = {}
    if re.search(r"\bcredit transactions?\b", lowered) or "is_credit = true" in lowered:
        facets["is_credit"] = True
    elif "debit transactions" in lowered or "is_credit = false" in lowered:
        facets["is_credit"] = False
    account_type = _value_for(text, ("account_type", "account type"), limit=64)
    if account_type is not None:
        if account_type.upper() not in _ACCOUNT_TYPES:
            raise RuleQuestionError("rule_question_invalid_facet")
        facets["account_type"] = account_type.upper()
    scheme = _value_for(text, ("card scheme",), limit=64)
    if scheme is not None:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,63}", scheme) is None:
            raise RuleQuestionError("rule_question_invalid_facet")
        facets["card_scheme"] = scheme
    aci = _value_for(text, ("aci",), limit=8)
    if aci is not None:
        facets["aci"] = aci.upper()
    if re.search(r"\btransaction value\b", lowered):
        values = re.findall(
            r"\btransaction value\s+(?:of\s+)?([0-9]+(?:\.[0-9]+)?)\s+EUR\b",
            text,
            re.IGNORECASE,
        )
        if (
            len(re.findall(r"\btransaction value\b", lowered)) != 1
            or len(values) != 1
            or len(values[0]) > 40
        ):
            raise RuleQuestionError("rule_question_invalid_transaction_value")
        facets["transaction_value"] = values[0]
    code = _mcc_code(text, descriptions)
    if code is not None:
        facets["merchant_category_code"] = code
    return facets


__all__ = ["RuleQuestionError", "parse_rule_question"]
