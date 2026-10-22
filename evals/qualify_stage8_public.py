"""Public method diagnostics without reference-answer access or score mutation."""

import argparse
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.metrics import root_mean_squared_error
from sklearn.model_selection import train_test_split

from generaldataagent.evaluation.stage_eight import stage8_fingerprint
from generaldataagent.evaluation.stage_six import Task, digest, verify_sources
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis
from generaldataagent.evaluation.stage_six_import import build_identifier_preserving_database

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    verify_sources(ROOT / "data/stage6")
    questions = [
        json.loads(line)
        for line in (ROOT / "data/stage6/infi/da-dev-questions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    public_tasks = {
        str(q["id"]): Task(
            "infiagent-dabench",
            str(q["id"]),
            q["question"],
            str(q.get("constraints", "")) + "\n" + str(q.get("format", "")),
            str(q["level"]),
            q["file_name"],
        )
        for q in questions
    }
    records = []
    for key in ("5", "30", "69", "70", "255", "326", "644", "659", "27", "132", "575"):
        task = public_tasks[key]
        source = ROOT / "data/stage6/infi/da-dev-tables" / task.asset
        database = args.output / (key + ".sqlite")
        build_identifier_preserving_database({"data": source}, database)
        with sqlite3.connect(database) as db:
            cursor = db.execute("SELECT * FROM data")
            names = [d[0] for d in cursor.description]
            rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        if key in {"30", "70"}:
            features = (
                ["age", "bmi"]
                if key == "30"
                else [
                    "exper",
                    "looks",
                    "union",
                    "goodhlth",
                    "black",
                    "female",
                    "married",
                    "south",
                    "bigcity",
                    "smllcity",
                    "service",
                    "educ",
                ]
            )
            target = "charges" if key == "30" else "wage"
            columns = features + [target]
            experiment = {
                "features": features,
                "target": target,
                "estimator": "sklearn_linear_regression",
                "random_seed": 42,
                "split": {"test_fraction": 0.2 if key == "30" else 0.3},
                "missing_rows": "drop" if key == "30" else "raise",
                "standardize": False,
                "duplicate_policy": "report",
            }
            spec = {
                "table": "data",
                "columns": columns,
                "operation": "regression_evaluation",
                "experiment": experiment,
            }
            complete = [r for r in rows if all(r[c] is not None for c in columns)]
            x_train, x_test, y_train, y_test = train_test_split(
                [[r[c] for c in features] for r in complete],
                [r[target] for r in complete],
                test_size=experiment["split"]["test_fraction"],
                random_state=42,
            )
            independent = float(
                root_mean_squared_error(
                    y_test, LinearRegression().fit(x_train, y_train).predict(x_test)
                )
            )
            path = ["metrics", "rmse"]
        else:
            request = {"columns": [], "operation": "pearson"}
            if key in {"5", "69"}:
                inputs = ["SibSp", "Parch"] if key == "5" else ["exper", "looks"]
                name, target, op = (
                    ("FamilySize", "Fare", "add")
                    if key == "5"
                    else ("experience_score", "wage", "multiply")
                )
                request.update(
                    columns=[name, target],
                    features=[{"name": name, "operation": op, "columns": inputs}],
                )
                pairs = [
                    (
                        r[inputs[0]] + r[inputs[1]] if op == "add" else r[inputs[0]] * r[inputs[1]],
                        r[target],
                    )
                    for r in rows
                    if all(r[c] is not None for c in inputs + [target])
                ]
                independent = float(stats.pearsonr(*zip(*pairs, strict=True)).statistic)
                path = ["groups", 0, "correlation"]
            elif key == "659":
                request["columns"] = ["High", "Low"]
                request["method"] = {"numeric_coercion": "parse"}
                pairs = []
                for r in rows:
                    try:
                        pair = (float(r["High"]), float(r["Low"]))
                    except (ValueError, TypeError):
                        continue
                    if all(math.isfinite(v) for v in pair):
                        pairs.append(pair)
                independent = float(stats.pearsonr(*zip(*pairs, strict=True)).statistic)
                path = ["groups", 0, "correlation"]
            elif key in {"27", "132"}:
                column = "charges" if key == "27" else "Fare"
                values = [
                    r[column]
                    for r in rows
                    if isinstance(r[column], (int, float)) and math.isfinite(r[column])
                ]
                request.update(columns=[column], operation="outliers")
                if key == "27":
                    q1, q3 = np.percentile(values, [25, 75], method="linear")
                    independent = sum(
                        v < q1 - 1.5 * (q3 - q1) or v > q3 + 1.5 * (q3 - q1) for v in values
                    )
                    suffix = "iqr_outlier_count"
                else:
                    independent = sum(
                        abs((v - float(np.mean(values))) / float(np.std(values, ddof=1))) > 3
                        for v in values
                    )
                    suffix = "z_outlier_count"
                path = ["groups", 0, "statistics", column, suffix]
            elif key == "575":
                columns = ["AAPL.O", "MSFT.O", "AMZN.O"]
                request.update(
                    columns=["avg_price", ".SPX"],
                    features=[{"name": "avg_price", "operation": "row_mean", "columns": columns}],
                    decision_rule={
                        "alpha": 0.05,
                        "abs_r_threshold": 0.5,
                        "significant_high_label": "linear",
                        "significant_low_label": "nonlinear",
                        "nonsignificant_label": "none",
                    },
                )
                pairs = [
                    (float(np.mean([r[c] for c in columns])), r[".SPX"])
                    for r in rows
                    if all(
                        isinstance(r[c], (int, float)) and math.isfinite(r[c])
                        for c in columns + [".SPX"]
                    )
                ]
                independent = float(stats.pearsonr(*zip(*pairs, strict=True)).statistic)
                path = ["groups", 0, "correlation"]
            elif key == "255":
                request.update(columns=["gdpPercap_2007"], operation="summary")
                independent = float(
                    np.std(
                        [r["gdpPercap_2007"] for r in rows if r["gdpPercap_2007"] is not None],
                        ddof=1,
                    )
                )
                path = ["groups", 0, "statistics", "gdpPercap_2007", "std"]
            elif key == "644":
                request.update(
                    columns=["Close"],
                    operation="normality",
                    method={"normality": "shapiro"},
                    significance_rule={
                        "alpha": 0.05,
                        "significant_label": "Not a normal distribution",
                        "nonsignificant_label": "Normal distribution",
                    },
                )
                independent = float(
                    stats.shapiro([r["Close"] for r in rows if r["Close"] is not None]).pvalue
                )
                path = ["groups", 0, "statistics", "Close", "normality", "p_value"]
            else:
                from datetime import datetime

                request.update(
                    columns=["event_hour", "EVENTMSGTYPE"],
                    features=[
                        {"name": "event_hour", "operation": "hour", "columns": ["WCTIMESTRING"]}
                    ],
                )
                pairs = [
                    (datetime.strptime(r["WCTIMESTRING"], "%I:%M %p").hour, r["EVENTMSGTYPE"])
                    for r in rows
                    if r["WCTIMESTRING"] is not None and r["EVENTMSGTYPE"] is not None
                ]
                independent = float(stats.pearsonr(*zip(*pairs, strict=True)).statistic)
                path = ["groups", 0, "correlation"]
            spec = {
                "table": "data",
                "columns": request["columns"],
                "operation": "statistics",
                "statistics": request,
            }
        result = fixed_analysis(
            database,
            spec,
            allowed_relations={"data"},
            public_text=task.question + "\n" + task.instructions,
        )
        observed = result
        for part in path:
            observed = observed[part]
        if isinstance(independent, np.generic):
            independent = independent.item()
        records.append(
            {
                "case_id": key,
                "source_hash": digest(source),
                "independent_value": independent,
                "observed_value": observed,
                "passed": math.isclose(observed, independent, rel_tol=1e-10, abs_tol=1e-10),
                "public_method_only": True,
                "result": result,
            }
        )
    report = {
        "checks": records,
        "passed": all(r["passed"] for r in records),
        "real_model_calls": 0,
        "reference_answers_accessed": False,
        "source_integrity_scope": (
            "May hash label files; reference answer values are not decoded by method diagnostics"
        ),
        "strict_score_changed": False,
        "production_accepted": False,
        "source_fingerprints": stage8_fingerprint(ROOT),
        "dependency_lock_hash": digest(ROOT / "uv.lock"),
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"checks": len(records), "passed": report["passed"]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
