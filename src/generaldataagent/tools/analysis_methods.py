"""Recognize a narrow public Python mean/median requirement and verify method receipts."""

import re


def requests_python_statistics(text: str) -> bool:
    from generaldataagent.tools.independent_test import requests_independent_test

    return bool(
        not requests_independent_test(text)
        and re.search(r"\bpython\b", text, re.I)
        and re.search(r"\bmean\b", text, re.I)
        and re.search(r"\bmedian\b", text, re.I)
        and re.search(r"\bbuilt[ -]?in\b|\bstandard[ -]library\b", text, re.I)
        and not re.search(r"\b(?:do not use|without|no)\s+python\b", text, re.I)
    )


def python_statistics_bindings(bindings: dict, evidence: dict) -> bool:
    return bool(bindings) and all(
        evidence.get(step, {}).get("method_id") == "describe"
        and len(path) == 3
        and path[0] == "statistics"
        and path[-1] in {"mean", "median"}
        for step, path in bindings.values()
    )
