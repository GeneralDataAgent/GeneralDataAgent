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
    matches = re.findall(pattern, text, re.I)
    digits = {int(value) if value.isdigit() else words[value.casefold()] for value in matches}
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
    for value in re.findall(r"保留\s*([0-9]+|[零一二两三四五六七八九十])\s*位小数", text):
        digits.add(int(value) if value.isdigit() else chinese[value])
    return digits


def validate_field_sources(fields: dict[str, AnswerFieldSpec], columns, question, instructions):
    public = question + "\n" + instructions
    if fields and set(fields) != set(columns):
        raise ValueError("per-field rules must cover exactly the declared answer columns")
    for field in fields.values():
        if field.source_excerpt and field.source_excerpt not in public:
            raise ValueError("field rule needs an exact public source excerpt")
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
        if field.method == "describe.sample_std":
            if method != "describe" or not path or path[-1] != "sample_std":
                raise ValueError("field method does not match sample-standard-deviation evidence")
        elif method != field.method:
            raise ValueError(
                f"field method must match computed method_id {method}; omit optional method rule"
            )
    if raw is None:
        if field.nullable is False:
            raise ValueError("field is not nullable")
        return raw
    if field.kind == "set":
        if not isinstance(raw, list):
            raise ValueError("field requires complete set evidence")
    elif field.kind == "string":
        if not isinstance(raw, str):
            raise ValueError("field requires a string; cast explicitly in the computation")
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
    return raw


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
        kind = (
            "string"
            if re.search(r"\bstring\b", description, re.I)
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
        precision = public_precisions(description)
        if kind is None or len(precision) > 1 or len(excerpt) > 2000:
            continue
        for key in columns:
            if placeholders.get(key) in defined:
                candidate = AnswerFieldSpec(
                    kind=kind,
                    decimals=next(iter(precision)) if precision else None,
                    source_excerpt=excerpt,
                )
                if key in rules and rules[key] != candidate:
                    return {}  # Conflicting public definitions need explicit clarification.
                rules[key] = candidate
    return rules if set(rules) == set(columns) else {}
