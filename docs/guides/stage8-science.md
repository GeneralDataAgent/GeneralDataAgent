# Stage 8 fixed statistics and predictive experiments

Stage 8 extends the existing science workbench with a target-free `AnalysisSnapshot`, immutable
derived snapshots, explicit statistics contracts and a versioned multivariate OLS template.
`DatasetSnapshot` still requires one real target. Use `promote_analysis_snapshot` only when a
predictive task supplies that target. Full-population group aggregates and target-derived features
are rejected at this promotion boundary; group aggregates remain descriptive analysis tools.

The feature language accepts named columns and a whitelist of arithmetic, ISO date parts and
group aggregates. It cannot execute Python, SQL expressions, imports or shell commands. Derived
snapshots preserve artifact lineage. Division by zero and missing arithmetic operands produce
null; numeric filters precede finite pairing. Group aggregates use the original source population
before filters. Computation is complete within explicit row/cell limits; over-limit inputs fail.

`StatisticsRequest` declares ddof (default 1), missing handling, alpha, strict IQR/Z thresholds
and normality method. Pearson uses paired finite values and a two-sided test; summaries use
columnwise finite values. Quartiles use linear inclusive interpolation, skewness is bias corrected,
and kurtosis is bias-corrected Fisher excess. Constant/small samples are explicit. Auto normality
uses Shapiro for 3–5000 rows and D'Agostino-Pearson above 5000; explicit D'Agostino requires 8
observations and warns below 20. These results do not verify distributional assumptions.

`ExperimentSpec(estimator="linear_regression", ...)` chooses template 2.0. The existing majority
and mean templates remain 1.0. OLS uses Householder QR with an intercept and rejects rank-deficient
designs. Imputation and optional standardization fit only training rows. The report contains raw
coefficients, train-only preprocessing values, complete split indices, held-out predictions,
MAE/RMSE/R² and the training-mean baseline. Features and target are locked before splitting;
no feature selection, hyperparameter search or automatic seed search occurs. Random, temporal
and group splits reuse the existing checks. Constant held-out targets reject undefined R².

`export_replayed_notebook` writes an editable `.ipynb` containing the exact fixed worker and JSON
input. It executes both cells again in the bounded runner and compares the complete result with
the original run before marking reproducibility. Replay accepts only exact repository-owned cell
source and the requested input manifest. Edited notebooks are not accepted for sandbox execution.
This is exact-cell execution, not independent Jupyter-kernel or cross-library certification.

The opt-in research profile is `sql-science`, under `stage8-v1`. It extends Stage 7 with
`analysis.operation="statistics"` and `"regression_evaluation"`, typed discovery, source/method
fingerprints and evidence binding. The legacy profile does not expose or execute these operations.
The research adapter uses the bounded local fixed-template runner, not production admission.
Production workbench mode still requires governed artifacts, independent audit and digest-pinned
OCI. Old images cannot execute template 2.0: build a new image using the existing pinned Dockerfile
and use its immutable digest. Image construction does not grant production approval.

Run local capability qualification:

```bash
uv run --frozen python evals/run_stage8_acceptance.py \
  --output .artifacts/stage8/local-qualification --public-sources
```

For actual OCI execution add `--oci-runtime path/to/runtime.json`, containing `engine` and
digest-pinned `image_ref`. This performs real executions, including Notebook replay, with the
existing no-network/read-only/non-root restrictions. Capability qualification reports zero model
calls and does not alter benchmark labels, strict scores or production acceptance.

For a fresh model comparison, both arms must use `--evaluation-protocol stage8-v1`, the same source
data, cohort, route, budgets and scorer. Use `sql-semantic` as the Stage 7 control and `sql-science`
as the candidate. Token caps remain unset by default. Preserve all historical-correct cases and
all regressions; never combine scores from different runs into a claimed paired result.

Method references: [SciPy Pearson](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.pearsonr.html),
[SciPy normaltest](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.normaltest.html),
[scikit-learn leakage guidance](https://scikit-learn.org/stable/common_pitfalls.html).
The exact installed SciPy/NumPy versions and dependency-lock hash are recorded by qualification;
the worker itself uses only the Python standard library for OCI isolation.
