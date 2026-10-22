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
        if re.search(r"(?:^|_)std(?:_|$)|stddev|stdev|deviation|standard_deviation", name, re.I)
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
            and (
                path[-1] in {"std", "z_inlier_std", "iqr_inlier_std"}
                or (not named and any(str(part).startswith("z_outlier_") for part in path))
            )
        )

    return bool(candidates) and (
        all(matches(binding) for _, binding in candidates)
        if named
        else any(matches(binding) for _, binding in candidates)
    )


def statistical_collection(result, path):
    """Only exact backend-generated complete outlier identifier paths are collectable."""
    if (
        len(path) != 7
        or path[0] != "groups"
        or type(path[1]) is not int
        or path[1] < 0
        or path[2] != "statistics"
        or path[4] not in {"z_outlier_identifiers", "iqr_outlier_identifiers"}
        or path[5] not in result.get("requested_identifiers", [])
        or path[6] != "items"
        or result.get("method_id") not in {"outliers", "summary"}
    ):
        return None
    try:
        summary = result["groups"][path[1]]["statistics"][path[3]]
        collection = summary[path[4]][path[5]]
        prefix = "z" if path[4].startswith("z_") else "iqr"
        if (
            collection.get("complete") is True
            and type(collection.get("total_count")) is int
            and isinstance(collection.get("items"), list)
            and collection.get("collection_kind") == "list"
            and collection.get("total_count") == summary[prefix + "_outlier_count"]
            and collection.get("total_count") == len(collection.get("items", [])) <= 10000
        ):
            return collection
    except (KeyError, IndexError, TypeError):
        pass
    return None
