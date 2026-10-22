"""Finite-population statistics and whitelist features; no eval or generated SQL."""

import hashlib
import json
import math
import operator
import statistics
import warnings
from collections import defaultdict
from datetime import date, datetime, time

import scipy
from scipy import stats

from generaldataagent.contracts.statistics import DerivedFeature, StatisticsRequest


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def derive_features(rows: list[dict], features: list[DerivedFeature]) -> list[dict]:
    """Evaluate ordered stateless expressions on a copy. Null arithmetic propagates.

    Group aggregates describe this snapshot only and must not be fitted on a full
    population before predictive splitting. The modeling path rejects such lineage.
    """
    result = [dict(row) for row in rows]
    schema = set(result[0]) if result else set()
    if any(set(row) != schema for row in result):
        raise ValueError("rows must share a schema")
    for feature in features:
        if feature.name in schema:
            raise ValueError("derived features cannot overwrite existing columns")
        if not set(feature.columns + feature.group_by) <= schema:
            raise ValueError("unknown feature input column")
        groups = defaultdict(list)
        if feature.group_by:
            for row in result:
                key = tuple((type(row[c]).__name__, row[c]) for c in feature.group_by)
                if finite_number(row[feature.columns[0]]):
                    groups[key].append(row[feature.columns[0]])
        for row in result:
            values = [row[c] for c in feature.columns]
            if feature.constant is not None:
                values.append(feature.constant)
            if feature.group_by:
                key = tuple((type(row[c]).__name__, row[c]) for c in feature.group_by)
                group = groups[key]
                functions = {
                    "group_mean": statistics.fmean,
                    "group_sum": math.fsum,
                    "group_median": statistics.median,
                    "group_count": len,
                }
                value = functions[feature.operation](group) if group else None
                if feature.operation == "group_count":
                    value = len(group)
            elif feature.operation in {"year", "month", "day", "hour", "minute", "second"}:
                if values[0] is None:
                    value = None
                else:
                    raw = str(values[0]).strip()
                    try:
                        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        try:
                            parsed = date.fromisoformat(raw)
                        except ValueError as exc:
                            if feature.operation not in {"hour", "minute", "second"}:
                                raise ValueError(
                                    "date feature requires an ISO date/datetime"
                                ) from exc
                            try:
                                parsed = time.fromisoformat(raw)
                            except ValueError:
                                parsed = None
                                for fmt in ("%I:%M %p", "%I:%M:%S %p"):
                                    try:
                                        parsed = datetime.strptime(raw, fmt)
                                        break
                                    except ValueError:
                                        continue
                                if parsed is None:
                                    raise ValueError("invalid ISO or AM/PM clock time") from exc
                    value = getattr(parsed, feature.operation)
            elif not all(finite_number(v) for v in values):
                value = None
            elif feature.operation == "row_mean":
                value = statistics.fmean(values)
            else:
                functions = {
                    "add": operator.add,
                    "subtract": operator.sub,
                    "multiply": operator.mul,
                    "divide": operator.truediv,
                }
                value = (
                    None
                    if feature.operation == "divide" and values[1] == 0
                    else (functions[feature.operation](*values))
                )
                if value is not None and not finite_number(value):
                    raise ValueError("derived feature overflow")
            row[feature.name] = value
        schema.add(feature.name)
    return result


def _number(value):
    value = float(value)
    return value if math.isfinite(value) else None


def _rows_hash(rows):
    # Preserve nonfinite provenance without emitting non-JSON NaN/Infinity.
    canonical = [
        {
            c: {"nonfinite": str(v)} if isinstance(v, float) and not math.isfinite(v) else v
            for c, v in row.items()
        }
        for row in rows
    ]
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
    )


def _summary(values, method):
    count = len(values)
    if not count:
        return {"count": 0, "status": "no_finite_values"}
    variable = len(set(values)) > 1
    std = statistics.stdev(values) if method.ddof else statistics.pstdev(values)
    quartiles = stats.scoreatpercentile(values, [25, 75], interpolation_method="fraction")
    q1, q3 = map(float, quartiles)
    iqr = q3 - q1
    mean = statistics.fmean(values)
    low, high = q1 - method.iqr_multiplier * iqr, q3 + method.iqr_multiplier * iqr
    output = {
        "count": count,
        "sum": math.fsum(values),
        "mean": mean,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "range": max(values) - min(values),
        "std": std,
        "variance": std * std,
        "q1": q1,
        "q3": q3,
        "iqr": iqr,
        "iqr_lower": low,
        "iqr_upper": high,
        "iqr_outlier_indices": [i for i, v in enumerate(values) if v < low or v > high],
        "z_outlier_indices": [
            i for i, v in enumerate(values) if std and abs((v - mean) / std) > method.z_threshold
        ],
        "skewness": _number(stats.skew(values, bias=False)) if variable and count >= 3 else None,
        "excess_kurtosis": _number(stats.kurtosis(values, fisher=True, bias=False))
        if variable and count >= 4
        else None,
        "status": "ok" if variable else "constant_column",
    }
    for kind in ("iqr", "z"):
        selected = [values[i] for i in output[kind + "_outlier_indices"]]
        output.update(
            {
                kind + "_outlier_count": len(selected),
                kind + "_outlier_mean": statistics.fmean(selected) if selected else None,
                kind + "_outlier_median": statistics.median(selected) if selected else None,
            }
        )
    return output


def compute_statistics(rows: list[dict], request: StatisticsRequest, *, public_text="") -> dict:
    source_hash = _rows_hash(rows)
    numeric_inputs = set(request.columns + [f.column for f in request.where])
    for feature in request.features:
        if feature.operation not in {"year", "month", "day", "hour", "minute", "second"}:
            numeric_inputs.update(feature.columns)
    parsed = [dict(row) for row in rows]
    if request.method.numeric_coercion == "parse":
        for row in parsed:
            for column in numeric_inputs & row.keys():
                value = row[column]
                if isinstance(value, str):
                    try:
                        number = float(value.strip().replace(",", ""))
                        row[column] = number if math.isfinite(number) else None
                    except ValueError:
                        row[column] = None
    derived = derive_features(parsed, request.features)
    schema = set(derived[0]) if derived else set()
    required = set(request.columns + request.group_by + [f.column for f in request.where])
    if not required <= schema:
        raise ValueError("statistics references unknown columns")
    comparisons = {
        "eq": operator.eq,
        "ne": operator.ne,
        "lt": operator.lt,
        "le": operator.le,
        "gt": operator.gt,
        "ge": operator.ge,
    }
    selected = [
        (i, row)
        for i, row in enumerate(derived)
        if all(
            finite_number(row[f.column]) and comparisons[f.operator](row[f.column], f.value)
            for f in request.where
        )
    ]
    groups = defaultdict(list)
    for i, row in selected:
        key = tuple((type(row[c]).__name__, row[c]) for c in request.group_by)
        groups[key].append((i, row))
    output_groups = []
    for key, entries in sorted(groups.items(), key=lambda item: repr(item[0])):
        group = {
            "group": {c: v[1] for c, v in zip(request.group_by, key, strict=True)},
            "rows": len(entries),
            "statistics": {},
        }
        if request.operation == "pearson":
            pairs = [
                (i, [r[c] for c in request.columns])
                for i, r in entries
                if all(finite_number(r[c]) for c in request.columns)
            ]
            if request.method.missing == "raise" and len(pairs) != len(entries):
                raise ValueError("nonfinite paired observation")
            if len(pairs) < 2 or any(len({v[j] for _, v in pairs}) < 2 for j in (0, 1)):
                raise ValueError("Pearson requires >=2 pairs and nonconstant columns")
            from generaldataagent.tools.pearson_test import pearson_test

            test = pearson_test(
                [v for _, v in pairs], rule=request.decision_rule, public_text=public_text
            )
            group.update(
                **test,
                observations=len(pairs),
                dropped_rows=len(entries) - len(pairs),
                source_indices=[i for i, _ in pairs],
                reject_null=bool(test["p_value"] < request.method.alpha),
            )
            if request.direction_rule:
                from generaldataagent.catalog.field_evidence import public_quoted_labels
                from generaldataagent.contracts.statistical_rules import PearsonDecisionRule
                from generaldataagent.tools.pearson_test import public_decision

                rule = request.direction_rule
                r, p = test["correlation"], test["p_value"]
                receipt = public_decision(
                    PearsonDecisionRule(
                        alpha=rule.alpha,
                        abs_r_threshold=rule.r_threshold,
                        significant_high_label=rule.positive_label,
                        significant_low_label=rule.negative_label,
                        nonsignificant_label=rule.nonsignificant_label,
                    ),
                    r,
                    p,
                    public_text,
                )
                if p >= rule.alpha:
                    label = rule.nonsignificant_label
                elif r >= rule.r_threshold:
                    label = rule.positive_label
                elif r <= -rule.r_threshold:
                    label = rule.negative_label
                elif rule.weak_label and rule.weak_label in public_quoted_labels(public_text):
                    label = rule.weak_label
                else:
                    raise ValueError(
                        "public directional policy lacks significant weak-correlation label"
                    )
                group.update(receipt, public_rule_label=label, direction_rule=rule.model_dump())
        else:
            for column in request.columns:
                pairs = [(i, row[column]) for i, row in entries if finite_number(row[column])]
                if request.method.missing == "raise" and len(pairs) != len(entries):
                    raise ValueError("nonfinite observation")
                values = [v for _, v in pairs]
                if len(values) <= request.method.ddof:
                    summary = {"count": len(values), "status": "insufficient_sample"}
                else:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        summary = _summary(values, request.method)
                summary.update(
                    dropped_rows=len(entries) - len(values), source_indices=[i for i, _ in pairs]
                )
                for kind in ("iqr", "z"):
                    if kind + "_outlier_indices" in summary:
                        summary[kind + "_outlier_source_indices"] = [
                            pairs[i][0] for i in summary[kind + "_outlier_indices"]
                        ]
                if request.operation == "normality":
                    method = request.method.normality
                    method = (
                        ("shapiro" if len(values) <= 5000 else "dagostino")
                        if method == "auto"
                        else method
                    )
                    minimum = 3 if method == "shapiro" else 8
                    if len(values) < minimum or len(set(values)) < 2:
                        summary["normality"] = {
                            "status": "insufficient_or_constant",
                            "method": method,
                        }
                    elif method == "shapiro" and len(values) > 5000:
                        raise ValueError(
                            "Shapiro p-value is not qualified for n>5000; use dagostino"
                        )
                    else:
                        test = (
                            stats.shapiro(values)
                            if method == "shapiro"
                            else stats.normaltest(values)
                        )
                        summary["normality"] = {
                            "status": "ok",
                            "method": method,
                            "statistic": _number(test.statistic),
                            "p_value": _number(test.pvalue),
                            "reject_null": bool(test.pvalue < request.method.alpha),
                            "small_sample_warning": method == "dagostino" and len(values) < 20,
                        }
                        if request.significance_rule:
                            from generaldataagent.tools.pearson_test import public_significance

                            summary["normality"].update(
                                public_significance(
                                    request.significance_rule, float(test.pvalue), public_text
                                )
                            )
                            summary["normality"]["decision_scope"] = (
                                "public normality p-value policy; assumptions unverified"
                            )
                group["statistics"][column] = summary
        output_groups.append(group)
    return {
        "operation": request.operation,
        "method_spec": request.method.model_dump(),
        "method_version": "2.0",
        "scipy_version": scipy.__version__,
        "source_hash": source_hash,
        "derived_hash": _rows_hash(derived),
        "source_rows": len(rows),
        "filtered_rows": len(selected),
        "groups": output_groups,
        "scope": "full bounded snapshot; no sampling; assumptions unverified",
        "pipeline": "derive on source -> filter -> columnwise/pairwise finite -> compute",
        "quantiles": "linear inclusive h=(n-1)*p; strict fences; unbiased skew/Fisher kurtosis",
    }
