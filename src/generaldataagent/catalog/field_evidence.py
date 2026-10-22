"""Conservative public field mappings and observed date-format candidates."""

import re
from datetime import datetime

from sqlglot import exp, parse_one


def public_name_mentioned(name: str, text: str) -> bool:
    """Lexical preview priority only, including simple English plural spellings.

    This never authorizes a relation, creates an alias, or certifies business identity.
    """
    if not name:
        return False
    forms = [name]
    if re.fullmatch(r"[A-Za-z]{2,100}", name):
        if re.search(r"[^aeiou]y$", name, re.I):
            forms.append(name[:-1] + "ies")
        elif re.search(r"(?:s|x|z|ch|sh)$", name, re.I):
            forms.append(name + "es")
        else:
            forms.append(name + "s")
    return bool(re.search(r"(?<!\w)(?:" + "|".join(map(re.escape, forms)) + r")(?!\w)", text, re.I))


def date_candidates(column: str, values: list, *, complete: bool) -> dict | None:
    if not values or not re.search(r"date|day|birth|time|日期|生日", column, re.I):
        return None
    patterns = {
        "iso_date": "%Y-%m-%d",
        "iso_datetime": "%Y-%m-%d %H:%M:%S",
        "compact_date": "%Y%m%d",
        "month_first_slash": "%m/%d/%Y",
        "day_first_slash": "%d/%m/%Y",
        "year_first_slash": "%Y/%m/%d",
    }
    candidates = []
    for name, pattern in patterns.items():
        valid = True
        for value in values:
            text = str(value)
            try:
                parsed = datetime.strptime(text, pattern)
                # strptime accepts non-padded values. Preserve the observed encoding accurately.
                if name in {"iso_date", "compact_date"} and parsed.strftime(pattern) != text:
                    valid = False
            except (ValueError, TypeError, OverflowError):
                valid = False
        if valid:
            candidates.append(name)
    return {
        "candidates": candidates,
        "observed_distinct_values": len(values),
        "observed_values_complete": complete,
        "verified_business_semantics": False,
    }


def public_field_mappings(text: str, schema: dict[str, list[str]]) -> dict:
    """Only explicit 'label means/refers to field[=value]' mappings are admitted.

    Unqualified columns occurring in several scoped relations remain ambiguous. Public mappings
    remain untrusted evidence and never modify the certified metric catalog.
    """
    mappings, ambiguous = [], []
    ascii_case = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    for sentence in re.split(r"[;\n]", text):
        match = re.fullmatch(
            r"\s*(.{1,150}?)\s+(?:means|refers to|表示|是指)\s+(.{1,250}?)\s*\.?", sentence, re.I
        )
        if not match:
            continue
        label, expression = match.groups()
        try:
            tree = parse_one("SELECT " + expression, read="sqlite")
        except Exception:
            continue
        if len(tree.selects) != 1:
            continue
        node = tree.selects[0]
        value = None
        literal_kind = None
        if isinstance(node, exp.EQ) and isinstance(node.expression, exp.Literal):
            literal_kind = "string" if node.expression.is_string else "number"
            value, node = node.expression.this, node.this
        if not isinstance(node, exp.Column) or node.db or node.catalog:
            continue
        candidates = [
            (table, field)
            for table, fields in schema.items()
            for field in fields
            if field.translate(ascii_case) == node.name.translate(ascii_case)
            and (not node.table or node.table.translate(ascii_case) == table.translate(ascii_case))
        ]
        record = {
            "label": label.strip(),
            "source_excerpt": sentence.strip(),
            "basis": "explicit_public_mapping",
        }
        if literal_kind is not None:
            record["literal_kind"] = literal_kind
        if len(candidates) != 1:
            if candidates:
                ambiguous.append(record | {"candidates": candidates, "enum_value": value})
            continue
        table, field = candidates[0]
        mappings.append(record | {"table": table, "column": field, "enum_value": value})
    return {"mappings": mappings, "ambiguous": ambiguous}


def public_quoted_labels(text: str) -> list[str]:
    """Read bounded public labels, keeping apostrophes in prose out of the quote grammar."""
    labels = {
        left or right for left, right in re.findall(r'"([^"\n]{1,160})"|“([^”\n]{1,160})”', text)
    }
    labels.update(
        match.replace("''", "'")
        for match in re.findall(r"(?<!\w)'((?:[^'\n]|''){1,160})'(?!\w)", text)
    )
    labels.update(re.findall(r"‘([^’\n]{1,160})’", text))
    return sorted(labels)


def enum_label_candidates(text: str, table: str, column: str, values: list, *, complete: bool):
    """Suggest observed spelling variants only for complete, bounded value domains.

    Case, spaces, hyphens and underscores may differ between public labels and data. A match
    is evidence to inspect, never permission to replace a filter or assert business equivalence.
    """
    if not complete:
        return []
    labels = public_quoted_labels(text)

    def normalized(value):
        return re.sub(r"[\s_-]+", "", value.casefold())

    output = []
    for label in labels:
        matches = sorted(
            {
                value
                for value in values
                if isinstance(value, str)
                and 0 < len(value) <= 160
                and normalized(value) == normalized(label)
            }
        )
        if not matches or matches == [label]:
            continue
        output.append(
            {
                "table": table,
                "column": column,
                "public_label": label,
                "observed_values": matches,
                "ambiguous": len(matches) != 1,
                "domain_complete": True,
                "domain_distinct_n": len(values),
                "basis": "observed_spelling_candidate",
                "verified_business_semantics": False,
            }
        )
    return output
