"""Evidence-bound answer types and public precision. Unit labels never convert values."""

import hashlib
import json
import math
import re
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from generaldataagent.contracts.answer_fields import AnswerFieldSpec


def public_precisions(text: str) -> set[int]:
    words = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
    }
    # Public formatting also uses "a float with two decimal places", without "round".
    pattern = r"\b(\d+|" + "|".join(words) + r")\s+decimal\s+places?\b"
    digits = set()
    for match in re.finditer(pattern, text, re.I):
        prefix = text[max(0, match.start() - 60) : match.start()]
        if re.search(
            r"\b(?:not|never|without)\s+"
            r"(?:(?:be\s+)?round(?:ed|ing)?\s+(?:to\s+)?|us(?:e|ing)\s+)?$",
            prefix,
            re.I,
        ):
            continue
        value = match[1]
        digits.add(int(value) if value.isdigit() else words[value.casefold()])
    chinese = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    for match in re.finditer(r"保留\s*([0-9]+|[零一二两三四五六七八九十])\s*位小数", text):
        if re.search(r"(?:不|不要|无需|禁止)\s*$", text[max(0, match.start() - 8) : match.start()]):
            continue
        value = match[1]
        digits.add(int(value) if value.isdigit() else chinese[value])
    return digits


def public_collection_separator(text: str, key: str) -> str | None:
    if re.search(r"\b(?:JSON\s+array|Python\s+list|native\s+list)\b", text, re.I):
        return None
    values = re.findall(r"@" + re.escape(key) + r"\[([^]\n]{1,200})\]", text)
    separators = set()
    for value in values:
        if "," not in value or not re.search(r"…|\.{3}", value):
            continue
        tail = value[value.index(",") + 1 :]
        if tail.startswith("\t"):
            continue
        separators.add(", " if tail.startswith(" ") else ",")
    return next(iter(separators)) if len(separators) == 1 else None


def public_scalar_domain(text: str) -> dict:
    """Only explicit inclusive ranges and quoted closed string alternatives are recognized."""
    domain = {}
    bounds = re.findall(r"\bbetween\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)\b", text, re.I)
    if len(bounds) == 1:
        low, high = map(float, bounds[0])
        if math.isfinite(low) and math.isfinite(high) and low <= high:
            domain.update(minimum=low, maximum=high)
    literal = r"""(?:"[^"\n]{1,160}"|'[^'\n]{1,160}')"""
    match = re.search(
        r"\b(?:can(?:\s+either)?\s+be|(?:must|should)\s+be\s+one\s+of)\s+("
        + literal
        + r"(?:\s*(?:,\s*(?:or\s+)?|or\s+)"
        + literal
        + r"){1,31})",
        text,
        re.I,
    )
    if match and not re.match(r"\s*(?:,|\bor\b|\betc\b|\.{3}|…)", text[match.end() :], re.I):
        choices = re.findall(r"""["']([^"'\n]{1,160})["']""", match[1])
        if len(set(choices)) == len(choices):
            domain["choices"] = choices
    return domain


def public_nullability(text: str) -> bool | None:
    allows = bool(
        re.search(
            r"\b(?:may|can)\s+(?:also\s+)?be\s+null\b|\bor\s+null\b"
            r"|(?:^|\bis\s+|\bare\s+)(?:(?:a|an|the)\s+)?nullable\b",
            text.strip(),
            re.I,
        )
    )
    rejects = bool(
        re.search(
            r"\b(?:must\s+not|cannot)\s+be\s+null\b|\bnot\s+nullable\b"
            r"|(?:^|\bis\s+|\bare\s+)(?:(?:a|an|the)\s+)?non[- ]?null(?:able)?\b",
            text.strip(),
            re.I,
        )
    )
    return allows if allows != rejects else None


def validate_field_sources(fields: dict[str, AnswerFieldSpec], columns, question, instructions):
    public = question + "\n" + instructions
    if fields and set(fields) != set(columns):
        raise ValueError("per-field rules must cover exactly the declared answer columns")
    for key, field in fields.items():
        if field.source_excerpt and field.source_excerpt not in public:
            raise ValueError("field rule needs an exact public source excerpt")
        domain = public_scalar_domain(field.source_excerpt)
        if field.nullable is True and public_nullability(field.source_excerpt) is not True:
            raise ValueError("nullable fields require an explicit public null allowance")
        for attribute in ("minimum", "maximum", "choices"):
            proposed = getattr(field, attribute)
            if proposed is not None and proposed != domain.get(attribute):
                raise ValueError("field value domain requires its explicit public definition")
        if (
            field.collection_separator is not None
            and public_collection_separator(field.source_excerpt, key) != field.collection_separator
        ):
            raise ValueError("collection presentation requires its explicit public format")
        if field.decimals is not None and public_precisions(field.source_excerpt) != {
            field.decimals
        }:
            raise ValueError("field precision requires its explicit public rounding rule")
        if field.unit:
            aliases = {
                "%": ["%", "percent", "percentage", "百分"],
                "years": ["age", "years", "how old", "岁"],
            }
            terms = aliases.get(field.unit.casefold(), [field.unit.casefold()])
            if not field.source_excerpt or not any(
                term in field.source_excerpt.casefold() for term in terms
            ):
                raise ValueError(
                    "unit label requires explicit public evidence; no implicit conversion"
                )


def validate_field_value(raw, field: AnswerFieldSpec, result: dict, path: list):
    method = result.get("method_id") or result.get("operation")
    if field.method:
        if field.method == "sklearn_linear_regression":
            if (
                method != "regression_evaluation"
                or result.get("model", {}).get("algorithm")
                != "sklearn.linear_model.LinearRegression"
                or not result.get("model", {}).get("sklearn_version")
            ):
                raise ValueError("field requires executed sklearn regression evidence")
        elif field.method == "describe.sample_std":
            if method != "describe" or not path or path[-1] != "sample_std":
                raise ValueError("field method does not match sample-standard-deviation evidence")
        elif method != field.method:
            raise ValueError(
                f"field method must match computed method_id {method}; omit optional method rule"
            )
    if raw is None:
        if field.nullable is False or (
            field.nullable is None
            and any(value is not None for value in (field.minimum, field.maximum, field.choices))
        ):
            raise ValueError("field is not nullable")
        return raw
    if field.kind in {"set", "list"}:
        if not isinstance(raw, list):
            raise ValueError("field requires complete collection evidence")
        if field.item_kind:
            item_rule = AnswerFieldSpec(kind=field.item_kind, nullable=False)
            raw = [validate_field_value(item, item_rule, {}, []) for item in raw]
        if field.order:
            try:
                ordered = sorted(raw, reverse=field.order == "desc")
            except TypeError:
                raise ValueError("collection ordering requires comparable values") from None
            if raw != ordered:
                raise ValueError("collection violates the public ordering requirement")
    elif field.kind == "string":
        if not isinstance(raw, str):
            raise ValueError("field requires a string; cast explicitly in the computation")
        if field.choices is not None and raw not in field.choices:
            raise ValueError("field violates the public closed string choices")
    elif field.kind == "boolean":
        if type(raw) is int and raw in (0, 1):
            raw = bool(raw)  # SQLite boolean predicates are 0/1; the field contract is explicit.
        elif type(raw) is not bool:
            raise ValueError("field requires boolean evidence")
    else:
        if type(raw) not in {int, float} or not math.isfinite(raw):
            raise ValueError("field requires finite numeric evidence")
        if field.kind == "integer":
            if int(raw) != raw:
                raise ValueError("nonintegral result cannot be presented as an integer")
            raw = int(raw)
        if field.minimum is not None and raw < field.minimum:
            raise ValueError("field is below the public minimum")
        if field.maximum is not None and raw > field.maximum:
            raise ValueError("field is above the public maximum")
    return raw


def present_collection(items: list, separator: str | None):
    if separator is None:
        return items
    if any(
        item is None
        or not isinstance(item, (str, int, float, bool))
        or any(mark in str(item) for mark in (",", "\n", "\r"))
        for item in items
    ):
        raise ValueError("collection items need an unambiguous public delimited presentation")
    return separator.join(str(item) for item in items)


def format_decimal(value: int | float, decimals: int) -> str:
    number = Decimal(str(value))
    with localcontext() as context:
        context.prec = max(
            34, len(number.as_tuple().digits) + abs(number.as_tuple().exponent) + decimals + 4
        )
        return format(
            number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_EVEN), f".{decimals}f"
        )


def binding_receipts(bindings, evidence, submitted, fields):
    receipts = {}
    for key, (step, path) in bindings.items():
        result = evidence[step]
        raw = result
        for part in path:
            raw = raw[part]
        rule = fields.get(key)
        receipts[key] = {
            "step": step,
            "path": path,
            "raw_value": raw,
            "submitted_value": submitted[key],
            "raw_type": type(raw).__name__,
            "submitted_type": type(submitted[key]).__name__,
            "method_id": result.get("method_id") or result.get("operation"),
            "declared_public_unit": rule.unit if rule else None,
            "result_hash": hashlib.sha256(
                json.dumps(result, sort_keys=True, default=str).encode()
            ).hexdigest(),
        }
    return receipts


def infer_public_field_rules(instructions: str, columns: list[str]) -> dict[str, AnswerFieldSpec]:
    """Read explicit @key[placeholder] type/precision definitions, never label contents."""
    declared = re.findall(r"@(\w+)\[([^]\n]{1,200})\]", instructions)
    placeholders = {}
    for key, value in declared:
        if key in placeholders and placeholders[key] != value:
            return {}
        placeholders[key] = value
    rules = {}
    # Some public formats define each field inside its own brackets instead of
    # referring to a later "where" clause. Keep the bracket span as provenance;
    # never borrow another field's precision or infer a type from its key.
    for key in columns:
        description = placeholders.get(key, "")
        precision = public_precisions(description)
        if re.search(r"\b(?:not|never|without|string|list|array|set|date)\b", description, re.I):
            continue
        count = bool(
            re.match(
                r"(?:number|count)\s+of\s+(?:entries|rows|records|items|observations)\b",
                description,
                re.I,
            )
        )
        rounded = bool(re.search(r"\brounded?\s+to\b", description, re.I))
        if (count and not precision) or (rounded and len(precision) == 1):
            rules[key] = AnswerFieldSpec(
                kind="integer" if count and not precision else "number",
                decimals=next(iter(precision)) if precision else None,
                source_excerpt=f"@{key}[{description}]",
            )
    no_duplicates = re.search(
        r"\b(?:no|without)\s+duplicates\b|\bnot\s+contain\s+(?:any\s+)?duplicates\b",
        instructions,
        re.I,
    )
    if no_duplicates and len(instructions.strip()) <= 2000:
        for key in columns:
            placeholder = placeholders.get(key, "")
            if "," in placeholder and re.search(r"…|\.{3}", placeholder):
                rules[key] = AnswerFieldSpec(
                    kind="set",
                    nullable=False,
                    source_excerpt=instructions.strip(),
                    collection_separator=public_collection_separator(instructions, key),
                )
    # A public format may use "where" once, followed by several quoted definitions.
    # Keep spans in the original text so every rule retains exact provenance.
    quoted = r"[\"'][^\"'\n]{1,200}[\"']"
    subjects = quoted + r"(?:\s*(?:,\s*(?:and\s+)?|and\s+)" + quoted + r"){0,19}"
    definitions = list(
        re.finditer(
            r"(?:\bwhere\s+)?(?P<names>" + subjects + r")\s+(?:is|are)\s+"
            r"|\bwhere\s+(?P<plain>\w+)\s+(?:is|are)\s+",
            instructions,
            re.I,
        )
    )
    for index, match in enumerate(definitions):
        end = definitions[index + 1].start() if index + 1 < len(definitions) else len(instructions)
        excerpt = instructions[match.start() : end].strip()
        names = match.group("names") or match.group("plain")
        description = instructions[match.end() : end]
        defined = set(re.findall(r"[\"']([^\"'\n]{1,200})[\"']", names))
        if not defined and re.fullmatch(r"\w+", names.strip()):
            defined = {names.strip()}
        prefix = r"^(?:(?:a|an|the)\s+)?(?:(?:nullable|non[- ]?null(?:able)?)\s+)?"
        collection = bool(
            re.search(
                prefix + r"(?:list|collection|array|JSON\s+array|Python\s+list|native\s+list)\b",
                description.strip(),
                re.I,
            )
        )
        kind = (
            "set"
            if re.search(prefix + r"set\b", description.strip(), re.I)
            or no_duplicates
            and collection
            else "string"
            if re.search(r"\bstring\b", description, re.I)
            else "boolean"
            if re.search(r"\bboolean\b", description, re.I)
            else "integer"
            if re.search(r"\binteger\b", description, re.I)
            else "number"
            if re.search(
                r"\bnumbers?\b|\bfloat(?:ing)?\b|\bdecimal\b|\breal\s+(?:value|number)\b",
                description,
                re.I,
            )
            else None
        )
        item_kind = None
        if collection:
            kind = "set" if no_duplicates else "list"
        if kind in {"set", "list"}:
            element = re.search(
                r"\b(?:set|list|collection|array)\s+of\s+"
                r"(integers?|strings?|booleans?|numbers?|float(?:ing)?(?:\s+point)?)\b",
                description,
                re.I,
            )
            if element:
                word = element[1].lower()
                item_kind = (
                    "integer"
                    if word.startswith("integer")
                    else "string"
                    if word.startswith("string")
                    else "boolean"
                    if word.startswith("boolean")
                    else "number"
                )
        order = None
        if kind in {"set", "list"}:
            ascending = bool(re.search(r"\bascending\b", description, re.I))
            descending = bool(re.search(r"\bdescending\b", description, re.I))
            if ascending != descending:
                order = "asc" if ascending else "desc"
        precision = public_precisions(description)
        if kind is None or len(precision) > 1 or len(excerpt) > 2000:
            continue
        for key in columns:
            if placeholders.get(key) in defined:
                candidate = AnswerFieldSpec(
                    kind=kind,
                    item_kind=item_kind,
                    order=order,
                    nullable=(
                        public_nullability(description)
                        if public_nullability(description) is not None
                        else False
                        if kind in {"set", "list"}
                        else None
                    ),
                    decimals=next(iter(precision))
                    if precision and kind not in {"set", "list"}
                    else None,
                    source_excerpt=excerpt,
                    **{
                        name: value
                        for name, value in public_scalar_domain(description).items()
                        if (name == "choices" and kind == "string")
                        or (name != "choices" and kind in {"integer", "number"})
                    },
                )
                if key in rules and rules[key] != candidate:
                    return {}  # Conflicting public definitions need explicit clarification.
                rules[key] = candidate
    return rules if set(rules) == set(columns) else {}
