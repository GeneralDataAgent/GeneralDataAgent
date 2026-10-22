"""Opt-in Stage 8 capability discovery and source fingerprints; no label access."""

import hashlib
import json
import re
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest

STAGE8_PROMPT = """
Prefer the dedicated flat tools compute_statistics and evaluate_regression. Supply table and the
advertised method fields directly; the runtime constructs its internal analysis envelope.
compute_statistics columns are the OUTPUT numeric columns after feature derivation, not every
input operand. Example: table:"data",columns:["avg","target"],operation:"pearson",
features:[{name:"avg",operation:"row_mean",columns:["x","z","w"]}].
Operations: summary, outliers, normality, pearson. Features: add/subtract/multiply/divide,
row_mean, year/month/day/hour/minute/second, group_mean/group_sum/group_median/group_count.
Arithmetic may use one column and a right-hand constant. Missing operands propagate null.
method declares ddof (default sample=1), missing:"omit"/"raise", alpha, z_threshold (default 3),
iqr_multiplier (default 1.5), normality:"auto"/"shapiro"/"dagostino",
numeric_coercion:"strict"/"parse".
where numeric filters apply after derivation and before finite pairing; group_by returns groups.
For outliers, identifiers:["id"] and identifier_order:"asc" return complete identifier lists.
Each numeric summary has z/iqr_outlier_count/mean/median, z/iqr_inlier_count/mean/median/std and
z/iqr_outlier_identifiers[id].items. Bind these executed values, not hand-coded quartiles.
For statistical identifier lists use path ["groups",0,"statistics","measure",
"z_outlier_identifiers","id","items"]. No arbitrary list-valued paths are accepted.
Pearson gives r and two-sided p_value, with assumptions unverified. For public absolute-r/p
labels pass decision_rule:{alpha,abs_r_threshold,significant_high_label,significant_low_label,
nonsignificant_label}. For signed positive/negative labels use direction_rule instead, including
an explicit weak_label if needed. Normality accepts significance_rule. All labels and thresholds
require original public sources. Labels implement public policies, not proof of scientific form.
evaluate_regression requires features, target, estimator and primary_metric (mae/mse/rmse/r2).
Use estimator:"sklearn_linear_regression" for the declared default ML backend and when sklearn
is requested; "linear_regression" is standard-library QR. For descriptive full-data
slope/intercept, use legacy fixed_analysis linear_regression; do not introduce a holdout when
none is requested. Set split:{strategy:"random",test_fraction:0.2}, random_seed as publicly
specified. Group/temporal splits require group_column/timestamp_column. Preprocessing fits only
training rows. missing_rows:"drop" explicitly removes incomplete selected rows before splitting;
missing_features:"train_mean" instead imputes from training data. standardize:false disables
scaling. Never infer hidden seeds or choose features after viewing test performance.
Cross-split duplicates reject by default; local diagnostic duplicate_policy:"report" retains all
rows and the warning. This does not establish entity independence or production acceptance.
For requested standard deviation, bind describe.sample_std or a summary/inlier std receipt.
Default ddof=1; ddof=0 requires explicit population convention. Do not use SQL moment-subtraction
formulas as receipts. Z-score summaries carry the declared ddof without requiring an extra output.
submit_answers only needs name, step and path. Optional rule declarations require public source;
method evidence is enforced by the runtime, so do not add self-written method claims.
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
            r"\b(?:new feature|feature engineering|machine learning|linear regression|"
            r"least squares|OLS|outliers?|interquartile|IQR|"
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
    if "regression_requires_requested_metric_binding" in text or "primary metric conflicts" in text:
        return (
            "Use evaluate_regression with the publicly requested primary_metric. "
            "MSE binds metrics.mse, RMSE metrics.rmse; they are different quantities. "
            "Bind the actual requested metric instead of renaming or manually squaring an answer."
        )
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
