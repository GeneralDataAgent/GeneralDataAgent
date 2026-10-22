"""Execute deterministic Stage 8 capability checks; never claim an agent score."""

import argparse
import json
import math
import sqlite3
from pathlib import Path
from uuid import uuid4

import duckdb
import numpy as np
import scipy

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import (
    ExperimentSpec,
    OCIContainerRuntime,
    SandboxQuota,
    SnapshotColumn,
    SplitSpec,
)
from generaldataagent.contracts.statistics import DerivedFeature, MethodSpec, StatisticsRequest
from generaldataagent.data_science.analysis_snapshot import (
    create_analysis_snapshot,
    derive_analysis_snapshot,
    load_analysis_rows,
    promote_analysis_snapshot,
)
from generaldataagent.data_science.checks import split_indices
from generaldataagent.data_science.notebook import export_replayed_notebook
from generaldataagent.data_science.sandbox import FixedTemplateSandbox, OCIContainerSandbox
from generaldataagent.data_science.statistics import compute_statistics
from generaldataagent.data_science.workbench import DataScienceWorkbench
from generaldataagent.evaluation.stage_eight import stage8_fingerprint
from generaldataagent.evaluation.stage_six import digest, verify_sources
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis
from generaldataagent.evaluation.stage_six_import import build_identifier_preserving_database

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oci-runtime", type=Path)
    parser.add_argument("--public-sources", action="store_true")
    parser.add_argument("--include-sklearn", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    store = LocalArtifactStore(args.output / "artifacts")
    runner = (
        OCIContainerSandbox(OCIContainerRuntime.model_validate_json(args.oci_runtime.read_text()))
        if args.oci_runtime
        else FixedTemplateSandbox()
    )
    records = []

    def record(name, passed, **evidence):
        records.append({"name": name, "passed": bool(passed), **evidence})

    source = create_analysis_snapshot(
        task_id=uuid4(),
        tenant_id="stage8",
        project_id="qualification",
        data_domain="synthetic",
        source_name="deterministic multivariate fixture",
        rows=[
            {
                "x": float(i),
                "z": float(i * i % 13),
                "y": float(2 * i - 3 * (i * i % 13) + 7),
                "group": str(i // 5),
                "time": i,
            }
            for i in range(40)
        ],
        columns=tuple(
            SnapshotColumn(name=n, data_type=t, role=r)
            for n, t, r in [
                ("x", "number", "feature"),
                ("z", "number", "feature"),
                ("y", "number", "ignored"),
                ("group", "string", "group"),
                ("time", "integer", "timestamp"),
            ]
        ),
        artifact_store=store,
    )
    derived = derive_analysis_snapshot(
        snapshot=source,
        features=[DerivedFeature(name="xz", operation="multiply", columns=["x", "z"])],
        artifact_store=store,
    )
    record(
        "immutable_derived_snapshot",
        "xz" not in load_analysis_rows(source, store)[0]
        and derived.data_artifact.lineage == (source.data_artifact.artifact_id,),
        source=source.model_dump(mode="json"),
        derived=derived.model_dump(mode="json"),
    )
    snapshot = promote_analysis_snapshot(
        snapshot=derived,
        target="y",
        features=["x", "z"],
        group_column="group",
        timestamp_column="time",
        artifact_store=store,
    )
    spec = ExperimentSpec(
        task_id=snapshot.task_id,
        snapshot_id=snapshot.snapshot_id,
        problem_type="regression",
        target_column="y",
        feature_columns=("x", "z"),
        baseline="mean",
        primary_metric="r2",
        estimator="linear_regression",
    )
    rows = load_analysis_rows(source, store)
    specifications = [spec]
    if args.include_sklearn:
        specifications.append(
            spec.model_copy(
                update={
                    "estimator": "sklearn_linear_regression",
                    "quota": SandboxQuota(memory_mb=512, timeout_seconds=60, cpu_seconds=30),
                }
            )
        )
    for current_spec in specifications:
        for strategy in ("random", "temporal", "group"):
            active = current_spec.model_copy(
                update={
                    "split": SplitSpec(
                        strategy=strategy,
                        group_column="group" if strategy == "group" else None,
                        timestamp_column="time" if strategy == "temporal" else None,
                    )
                }
            )
            report = DataScienceWorkbench(artifact_store=store, sandbox=runner).run_experiment(
                snapshot=snapshot,
                spec=active,
                user_id="local-qualifier",
                tenant_id="stage8",
                project_id="qualification",
                data_domain="synthetic",
            )
            raw = json.loads(store.read_bytes(report.output_artifact))
            train, test = split_indices(rows, active)
            expected = np.linalg.lstsq(
                np.array([[1, rows[i]["x"], rows[i]["z"]] for i in train]),
                np.array([rows[i]["y"] for i in train]),
                rcond=None,
            )[0]
            record(
                current_spec.estimator + "_" + strategy,
                np.allclose(
                    [
                        raw["model"]["intercept"],
                        raw["model"]["coefficients"]["x"],
                        raw["model"]["coefficients"]["z"],
                    ],
                    expected,
                )
                and raw["metrics"]["r2"] > 0.999999,
                report=report.model_dump(mode="json"),
                independent_coefficients=expected.tolist(),
            )
    notebook = export_replayed_notebook(
        snapshot=snapshot, spec=spec, artifact_store=store, sandbox=runner
    )
    record(
        "notebook_exact_cell_replay",
        json.loads(store.read_bytes(notebook))["metadata"]["generaldataagent"]["reproducible"],
        artifact=notebook.model_dump(mode="json"),
    )
    if args.include_sklearn:
        notebook = export_replayed_notebook(
            snapshot=snapshot, spec=specifications[-1], artifact_store=store, sandbox=runner
        )
        record(
            "sklearn_notebook_exact_cell_replay",
            json.loads(store.read_bytes(notebook))["metadata"]["generaldataagent"]["reproducible"],
            artifact=notebook.model_dump(mode="json"),
        )
    for ddof in (0, 1):
        result = compute_statistics(
            [{"x": v} for v in [1, 2, 3]],
            StatisticsRequest(columns=["x"], method=MethodSpec(ddof=ddof)),
        )
        std = result["groups"][0]["statistics"]["x"]["std"]
        record(
            "ddof_" + str(ddof),
            math.isclose(std, float(np.std([1, 2, 3], ddof=ddof))),
            result=result,
        )
    normal = compute_statistics(
        [{"x": float(i)} for i in range(10)],
        StatisticsRequest(columns=["x"], operation="normality"),
    )
    record(
        "normality_sample_policy",
        normal["groups"][0]["statistics"]["x"]["normality"]["method"] == "shapiro",
        result=normal,
    )
    if args.public_sources:
        verify_sources(ROOT / "data/stage6")
        tables = ROOT / "data/stage6/infi/da-dev-tables"
        for case, filename, columns, features in [
            (
                "69",
                "beauty and the labor market.csv",
                ["experience_score", "wage"],
                [
                    {
                        "name": "experience_score",
                        "operation": "multiply",
                        "columns": ["exper", "looks"],
                    }
                ],
            ),
            ("255", "gapminder_gdp_asia.csv", ["gdpPercap_2007"], []),
        ]:
            path = tables / filename
            database = args.output / (case + ".sqlite")
            build_identifier_preserving_database({"data": path}, database)
            before = digest(database)
            result = fixed_analysis(
                database,
                {
                    "table": "data",
                    "columns": columns,
                    "operation": "statistics",
                    "statistics": {
                        "columns": columns,
                        "features": features,
                        "operation": "pearson" if case == "69" else "summary",
                    },
                },
                allowed_relations={"data"},
            )
            with sqlite3.connect(database) as db:
                cursor = db.execute("SELECT * FROM data")
                names = [d[0] for d in cursor.description]
                oracle_rows = cursor.fetchall()
            with duckdb.connect() as oracle:
                oracle.execute(
                    "CREATE TABLE data("
                    + ",".join('"' + n.replace('"', '""') + '" VARCHAR' for n in names)
                    + ")"
                )
                oracle.executemany(
                    "INSERT INTO data VALUES (" + ",".join("?" for _ in names) + ")", oracle_rows
                )
                query = (
                    "SELECT corr(CAST(exper AS DOUBLE)*CAST(looks AS DOUBLE),"
                    "CAST(wage AS DOUBLE)) FROM data"
                    if case == "69"
                    else "SELECT avg(CAST(gdpPercap_2007 AS DOUBLE)),"
                    "stddev_samp(CAST(gdpPercap_2007 AS DOUBLE)) FROM data"
                )
                expected = oracle.execute(query).fetchone()
            observed = (
                [result["groups"][0]["correlation"]]
                if case == "69"
                else [result["groups"][0]["statistics"][columns[0]][k] for k in ("mean", "std")]
            )
            record(
                "public_case_" + case,
                all(
                    math.isclose(a, b, rel_tol=1e-10)
                    for a, b in zip(observed, expected, strict=True)
                )
                and before == digest(database),
                source_hash=digest(path),
                independent_values=expected,
                observed_values=observed,
                source_rows=result["source_rows"],
                strict_label_access=False,
            )
    result = {
        "phase": "stage8",
        "scope": "deterministic capability qualification",
        "passed": all(r["passed"] for r in records),
        "checks": records,
        "runtime": "oci" if args.oci_runtime else "fixed_subprocess",
        "runtime_fingerprint": runner.runtime_fingerprint,
        "scipy_version": scipy.__version__,
        "numpy_version": np.__version__,
        "source_fingerprints": stage8_fingerprint(ROOT),
        "dependency_lock_hash": digest(ROOT / "uv.lock"),
        "real_model_calls": 0,
        "production_accepted": False,
        "official_leaderboard_comparable": False,
    }
    (args.output / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps({"passed": result["passed"], "checks": len(records), "output": str(args.output)})
    )
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
