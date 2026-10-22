"""Opt-in Stage 8 capability discovery and source fingerprints; no label access."""

import hashlib
import json
import re
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest

STAGE8_PROMPT = """
For derived features or explicit ddof use operation="statistics", with statistics={
columns:["interaction","target"],operation:"pearson",features:[{name:"interaction",
operation:"multiply",columns:["x","z"]}],method:{ddof:1,missing:"omit"}}.
Outer columns must equal statistics.columns. This computes on ALL rows, never a preview.
Statistics operations: summary, outliers, normality, pearson (coefficient and two-sided p).
Features whitelist: add/subtract/multiply/divide (two columns), year/month/day (one ISO date),
hour/minute/second (ISO datetime or ISO/AM-PM clock time, converted to 24-hour wall-clock),
group_mean/group_sum/group_median/group_count (one numeric column plus group_by).
For a per-row average use row_mean with columns:["x","z","w"]. Arithmetic supports a right
literal operand: {name:"average",operation:"divide",columns:["sum"],constant:3.0}.
Row means require every operand finite; missing arithmetic propagates null before pairing.
Expressions are ordered, cannot overwrite columns, null arithmetic propagates, division by
zero becomes null. Optional where:[{column,operator:"gt",value:0.0}] filters AFTER derivation,
BEFORE finite pairing. group_by:["category"] returns separate group statistics.
method includes ddof:0 or 1, missing:"omit" or "raise", alpha:0.05, z_threshold:3.0,
iqr_multiplier:1.5,normality:"auto"/"shapiro"/"dagostino". Summary exposes std, median,
range, IQR, unbiased skewness/excess kurtosis and source row indices; do not infer from samples.
For outliers use statistics.operation:"outliers". Each summary exposes iqr_outlier_count,
iqr_outlier_mean, iqr_outlier_median and z_outlier_count/z_outlier_mean/z_outlier_median.
Compute the explicitly requested IQR or Z-score method; do not hand-code approximate quartiles.
Pearson statistics supports the existing public decision_rule with alpha,abs_r_threshold,
significant_high_label,significant_low_label,nonsignificant_label. Quoted labels and thresholds
must occur in public text; the label is a public policy, not verified scientific linearity.
For real multivariate holdout use operation:"regression_evaluation", columns:["x","z","y"],
experiment:{features:["x","z"],target:"y",random_seed:42,split:{strategy:"random",test_fraction:0.2},
missing_features:"train_mean",standardize:true}. Group split requires group_column;
temporal split requires timestamp_column. Declare defaults and sensitivity if public requirements
do not fix seed or split. Optional derived uses the same whitelist but prohibits full-population
group aggregates and target-derived features. Preprocessing fits training rows only; returns
coefficients, held-out predictions/MAE/RMSE/R2, mean baseline, split indices and source hash.
For an explicit scikit-learn requirement set experiment.estimator:"sklearn_linear_regression".
This uses sklearn.model_selection.train_test_split for random splits and sklearn LinearRegression.
The stdlib linear_regression estimator uses Python shuffling and QR; it does not satisfy sklearn.
Set missing_rows:"drop" only when requested to drop rows before splitting; default rejects missing
targets/features unless train-only imputation is explicitly selected. For no preprocessing set
standardize:false. Do not infer hidden seeds. Normality may use significance_rule with alpha,
significant_label,nonsignificant_label; both quoted labels and alpha must occur in public text.
When a requested numeric column is stored as text (for example literal "null" cells), statistics
may explicitly use method.numeric_coercion:"parse". Only requested numeric inputs are parsed;
unparseable/nonfinite values are omitted or rejected according to missing policy. Group/date
columns are not automatically coerced. Default numeric_coercion:"strict" preserves types.
Experiments reject cross-split identical feature/target rows by default. Only for an explicit
local diagnostic evaluation that retains original rows, duplicate_policy:"report" records a warning
without deduplication; it does not establish entity independence or production acceptance.
Pearson may use direction_rule:{alpha:0.05,r_threshold:0.5,positive_label:"positive",
negative_label:"negative",nonsignificant_label:"none"}; all numbers and quoted labels require
public sources. A significant weak correlation requires an explicit public weak_label;
the tool refuses to invent it when absent.
For submit_answers, rule is optional: omit it when public fields already have types/rounding.
If present it requires kind; method must match the executed method_id (regression_evaluation,
sklearn_linear_regression, summary, normality, etc.). Do not invent library/function method names.
For standard deviation, use describe.sample_std or statistics summary.std, not a manual SQL
sqrt(E[x*x]-E[x]**2) expression. The declared default is sample ddof=1; use ddof=0 only for an
explicit population/ddof=0 requirement. Bind std to that method receipt; report conventions and
sensitivity when the public task leaves them unspecified. Do not infer conventions from labels.
"""


def needs_science_tools(public_text: str, textual_columns=()) -> bool:
    """Discover additional methods from public requirements, never case IDs or labels."""
    numeric_text = bool(
        re.search(r"\b(?:correlation|mean|deviation|variance)\b", public_text, re.I)
    ) and any(
        re.search(r"(?<!\w)" + re.escape(c) + r"(?!\w)", public_text, re.I) for c in textual_columns
    )

    return numeric_text or bool(
        re.search(
            r"\b(?:new feature|feature engineering|machine learning|linear regression model|"
            r"predict\w*|scikit[ -]?learn|sklearn|shapiro|normal distribution|normality|"
            r"skewness|kurtosis|standard[ _-]deviation|z[ -]?score|derived|ddof)\b|"
            r"派生|偏度|峰度|正态|建模",
            public_text,
            re.I,
        )
    )


def science_observation(result):
    """Summarize long index/prediction lists while preserving the full evidence result."""

    def compact(value, path):
        if isinstance(value, dict):
            return {k: compact(v, path + [k]) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            if len(value) > 20:
                return {
                    "count": len(value),
                    "preview": value[:5],
                    "truncated": True,
                    "full_result_path": path,
                    "sha256": hashlib.sha256(
                        json.dumps(value, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                }
            return [compact(v, path + [i]) for i, v in enumerate(value)]
        return value

    return compact(result, [])


def science_repair_hint(exc):
    text = str(exc)
    if "standard_deviation_requires_fixed_method_binding" in text:
        return (
            "Use fixed_analysis describe and bind statistics[column].sample_std (ddof=1 "
            "default), or statistics summary.std with the explicit method_spec. A manual SQL "
            "sqrt(E[x*x]-E[x]**2) is population ddof=0 and numerically unstable. Population "
            "conventions must be publicly explicit; never infer them from expected labels."
        )
    if "cross_split_duplicates" in text:
        return (
            "Identical selected feature/target rows cross the split. Default policy rejects. "
            "Use entity/group isolation when required. A local diagnostic may explicitly use "
            "duplicate_policy='report' while retaining every original row and the warning; "
            "never silently deduplicate or claim independent predictive acceptance."
        )
    if "sklearn" in text:
        return (
            "An explicit sklearn requirement needs regression_evaluation with "
            "estimator='sklearn_linear_regression' and evidence from that executed result. "
            "Use the publicly specified seed/split; stdlib fits and SQL cannot certify sklearn."
        )
    return None


def stage8_fingerprint(root: Path) -> dict:
    files = [
        "contracts/answer_fields.py",
        "tools/answer_fields.py",
        "tools/statistical_methods.py",
        "contracts/statistics.py",
        "contracts/data_science.py",
        "contracts/analysis.py",
        "data_science/statistics.py",
        "data_science/regression.py",
        "data_science/_worker.py",
        "data_science/sandbox.py",
        "data_science/checks.py",
        "data_science/snapshot.py",
        "data_science/analysis_snapshot.py",
        "data_science/notebook.py",
        "evaluation/stage_eight.py",
        "evaluation/stage_seven.py",
        "evaluation/stage_six_analysis.py",
        "evaluation/stage_six.py",
    ]
    return {name: digest(root / "src/generaldataagent" / name) for name in files}
