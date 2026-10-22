"""Public standard-deviation conventions and receipts, independent of benchmark labels."""

import re


def requests_standard_deviation(text):
    return bool(re.search(r"\bstandard[ _-]deviation\b|\bstddev\b|标准差", text, re.I))


def public_ddof(text):
    explicit = {int(v) for v in re.findall(r"\bddof\s*(?:=|:)?\s*([01])\b", text, re.I)}
    population = bool(
        re.search(
            r"\bpopulation\s+(?:standard\s+deviation|variance)\b|总体(?:标准差|方差)", text, re.I
        )
    )
    sample = bool(
        re.search(r"\bsample\s+(?:standard\s+deviation|variance)\b|样本(?:标准差|方差)", text, re.I)
    )
    conventions = explicit | ({0} if population else set()) | ({1} if sample else set())
    if len(conventions) > 1:
        raise ValueError("conflicting public ddof conventions")
    return next(iter(conventions)) if conventions else 1


def standard_deviation_bindings(bindings, evidence, text):
    if not requests_standard_deviation(text):
        return True
    expected = public_ddof(text)
    candidates = [
        (name, binding)
        for name, binding in bindings.items()
        if re.search(r"\bstd\b|stddev|stdev|deviation|standard_deviation", name, re.I)
    ]
    named = bool(candidates)
    if not named:
        candidates = list(bindings.items())

    def matches(binding):
        step, path = binding
        result = evidence.get(step, {})
        if not path:
            return False
        if result.get("method_id") in {"describe", "independent_t_test"}:
            return path[-1] == ("sample_std" if expected else "population_std")
        return (
            result.get("method_id") in {"summary", "outliers", "normality"}
            and result.get("method_spec", {}).get("ddof") == expected
            and path[-1] == "std"
        )

    return bool(candidates) and (
        all(matches(binding) for _, binding in candidates)
        if named
        else any(matches(binding) for _, binding in candidates)
    )
