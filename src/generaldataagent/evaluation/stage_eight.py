"""Opt-in Stage 8 capability discovery and source fingerprints; no label access."""

from pathlib import Path

from generaldataagent.evaluation.stage_six import digest

STAGE8_PROMPT = """
For derived features or explicit ddof use operation="statistics", with statistics={
columns:["interaction","wage"],operation:"pearson",features:[{name:"interaction",
operation:"multiply",columns:["exper","looks"]}],method:{ddof:1,missing:"omit"}}.
Outer columns must equal statistics.columns. This computes on ALL rows, never a preview.
Statistics operations: summary, outliers, normality, pearson (coefficient and two-sided p).
Features whitelist: add/subtract/multiply/divide (two columns), year/month/day (one ISO date),
group_mean/group_sum/group_median/group_count (one numeric column plus group_by).
Expressions are ordered, cannot overwrite columns, null arithmetic propagates, division by
zero becomes null. Optional where:[{column,operator:"gt",value:0.0}] filters AFTER derivation,
BEFORE finite pairing. group_by:["category"] returns separate group statistics.
method includes ddof:0 or 1, missing:"omit" or "raise", alpha:0.05, z_threshold:3.0,
iqr_multiplier:1.5,normality:"auto"/"shapiro"/"dagostino". Summary exposes std, median,
range, IQR, unbiased skewness/excess kurtosis and source row indices; do not infer from samples.
For real multivariate holdout use operation:"regression_evaluation", columns:["x","z","y"],
experiment:{features:["x","z"],target:"y",random_seed:42,split:{strategy:"random",test_fraction:0.2},
missing_features:"train_mean",standardize:true}. Group split requires group_column;
temporal split requires timestamp_column. Declare defaults and sensitivity if public requirements
do not fix seed or split. Optional derived uses the same whitelist but prohibits full-population
group aggregates and target-derived features. Preprocessing fits training rows only; returns
coefficients, held-out predictions/MAE/RMSE/R2, mean baseline, split indices and source hash.
"""


def stage8_fingerprint(root: Path) -> dict:
    files = [
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
