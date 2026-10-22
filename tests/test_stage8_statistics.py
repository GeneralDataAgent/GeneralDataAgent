import json
import sqlite3
from uuid import uuid4

import pytest
from scipy import stats

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import SnapshotColumn
from generaldataagent.contracts.statistics import DerivedFeature, MethodSpec, StatisticsRequest
from generaldataagent.data_science.analysis_snapshot import (
    create_analysis_snapshot,
    derive_analysis_snapshot,
    load_analysis_rows,
    promote_analysis_snapshot,
)
from generaldataagent.data_science.statistics import compute_statistics, derive_features
from generaldataagent.evaluation.stage_six_analysis import fixed_analysis


def test_feature_filter_pairing_matches_independent_library():
    rows = [
        {"x": 1, "y": 4, "z": 5},
        {"x": 2, "y": 3, "z": 7},
        {"x": 3, "y": 2, "z": 2},
        {"x": 4, "y": 5, "z": 1},
        {"x": None, "y": 3, "z": 6},
    ]
    request = StatisticsRequest.model_validate(
        {
            "columns": ["xy", "z"],
            "operation": "pearson",
            "features": [{"name": "xy", "operation": "multiply", "columns": ["x", "y"]}],
            "where": [{"column": "y", "operator": "gt", "value": 2.0}],
        }
    )
    result = compute_statistics(rows, request)
    group = result["groups"][0]
    independent = stats.pearsonr([4, 6, 20], [5, 7, 1])
    assert group["correlation"] == pytest.approx(independent.statistic)
    assert group["p_value"] == pytest.approx(independent.pvalue)
    assert group["observations"] == 3 and group["dropped_rows"] == 1
    assert group["source_indices"] == [0, 1, 3]
    assert "xy" not in rows[0]
    assert result == compute_statistics(rows, request)


@pytest.mark.parametrize("ddof, expected", [(0, (2 / 3) ** 0.5), (1, 1.0)])
def test_ddof_and_grouped_median_range(ddof, expected):
    rows = [{"g": "a", "x": x} for x in [1, 2, 3]] + [{"g": "b", "x": 5}] * 3
    request = StatisticsRequest(columns=["x"], group_by=["g"], method=MethodSpec(ddof=ddof))
    a, b = compute_statistics(rows, request)["groups"]
    assert a["statistics"]["x"]["std"] == pytest.approx(expected)
    assert a["statistics"]["x"]["median"] == 2
    assert a["statistics"]["x"]["range"] == 2
    assert b["statistics"]["x"]["status"] == "constant_column"
    assert b["statistics"]["x"]["skewness"] is None


def test_iqr_z_skew_and_distribution_small_large_samples():
    values = list(range(20)) + [1000]
    request = StatisticsRequest(columns=["x"], operation="outliers")
    summary = compute_statistics([{"x": x} for x in values], request)["groups"][0]["statistics"][
        "x"
    ]
    assert summary["iqr_outlier_indices"] == [20]
    assert summary["z_outlier_indices"] == [20]
    assert summary["skewness"] == pytest.approx(stats.skew(values, bias=False))
    assert summary["excess_kurtosis"] == pytest.approx(stats.kurtosis(values, bias=False))
    for count, method in [(2, "shapiro"), (10, "shapiro"), (5001, "dagostino")]:
        result = compute_statistics(
            [{"x": float(i)} for i in range(count)],
            StatisticsRequest(columns=["x"], operation="normality"),
        )
        normal = result["groups"][0]["statistics"]["x"]["normality"]
        assert normal["method"] == method
        assert normal["status"] == ("insufficient_or_constant" if count == 2 else "ok")


def test_whitelist_date_groups_null_zero_and_rejections():
    rows = [
        {"x": 4, "y": 0, "g": "a", "date": "2026-10-22"},
        {"x": 8, "y": 2, "g": "a", "date": None},
    ]
    features = [
        DerivedFeature(name="r", operation="divide", columns=["x", "y"]),
        DerivedFeature(name="m", operation="month", columns=["date"]),
        DerivedFeature(name="avg", operation="group_mean", columns=["x"], group_by=["g"]),
    ]
    derived = derive_features(rows, features)
    assert [r["r"] for r in derived] == [None, 4]
    assert [r["m"] for r in derived] == [10, None]
    assert [r["avg"] for r in derived] == [6, 6]
    for spec in [
        dict(name="bad", operation="exec", columns=["x"]),
        dict(name="a", operation="add", columns=["x"]),
        dict(name="a", operation="year", columns=["date"], group_by=["g"]),
    ]:
        with pytest.raises(ValueError):
            DerivedFeature.model_validate(spec)
    with pytest.raises(ValueError, match="overwrite"):
        derive_features(rows, [DerivedFeature(name="x", operation="year", columns=["date"])])
    with pytest.raises(ValueError, match="unknown"):
        derive_features(rows, [DerivedFeature(name="p", operation="year", columns=["absent"])])
    with pytest.raises(ValueError, match="nonfinite"):
        compute_statistics(
            [{"x": None}, {"x": 1}],
            StatisticsRequest(columns=["x"], method=MethodSpec(missing="raise")),
        )
    with pytest.raises(ValueError, match="nonconstant"):
        compute_statistics(
            [{"x": 1, "y": 2}] * 3, StatisticsRequest(columns=["x", "y"], operation="pearson")
        )


def test_immutable_target_free_snapshot_and_derived_lineage(tmp_path):
    store = LocalArtifactStore(tmp_path)
    source = create_analysis_snapshot(
        task_id=uuid4(),
        tenant_id="t",
        project_id="p",
        data_domain="d",
        source_name="demo",
        rows=[{"x": 1}, {"x": 2}],
        columns=(SnapshotColumn(name="x", data_type="number", role="feature"),),
        artifact_store=store,
    )
    features = [DerivedFeature(name="twice", operation="add", columns=["x", "x"])]
    derived = derive_analysis_snapshot(snapshot=source, features=features, artifact_store=store)
    assert load_analysis_rows(source, store) == [{"x": 1}, {"x": 2}]
    assert load_analysis_rows(derived, store) == [{"x": 1, "twice": 2}, {"x": 2, "twice": 4}]
    assert derived.data_artifact.lineage == (source.data_artifact.artifact_id,)
    assert derived.source_snapshot_id == source.snapshot_id
    path = store.root / derived.data_artifact.uri.removeprefix("artifact://local/")
    path.write_text("[]")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_analysis_rows(derived, store)


def test_sqlite_analysis_path_is_complete_read_only_and_serializable(tmp_path):
    path = tmp_path / "input.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table data(x REAL,y REAL)")
        db.executemany("insert into data values (?, ?)", [(1, 5), (2, 3), (3, 8)])
    before = path.read_bytes()
    result = fixed_analysis(
        path,
        {
            "table": "data",
            "columns": ["sum", "y"],
            "operation": "statistics",
            "statistics": {
                "columns": ["sum", "y"],
                "operation": "pearson",
                "features": [{"name": "sum", "operation": "add", "columns": ["x", "y"]}],
            },
        },
        allowed_relations={"data"},
    )
    assert result["source_rows"] == 3
    assert json.loads(json.dumps(result, allow_nan=False))["method_version"] == "2.0"
    assert path.read_bytes() == before


def test_nonfinite_omission_preserves_json_provenance():
    rows = [{"x": x} for x in [1, None, float("inf"), 3, float("nan")]]
    result = compute_statistics(rows, StatisticsRequest(columns=["x"]))
    summary = result["groups"][0]["statistics"]["x"]
    assert summary["source_indices"] == [0, 3]
    assert summary["dropped_rows"] == 3
    assert summary["mean"] == 2
    json.dumps(result, allow_nan=False)


def test_snapshot_promotion_rejects_aggregate_and_target_lineage(tmp_path):
    store = LocalArtifactStore(tmp_path)
    source = create_analysis_snapshot(
        task_id=uuid4(),
        tenant_id="t",
        project_id="p",
        data_domain="d",
        source_name="demo",
        rows=[{"x": 1, "y": 2, "g": "a"}, {"x": 2, "y": 3, "g": "a"}],
        columns=tuple(
            SnapshotColumn(name=n, data_type=t, role=r)
            for n, t, r in [
                ("x", "number", "feature"),
                ("y", "number", "ignored"),
                ("g", "string", "group"),
            ]
        ),
        artifact_store=store,
    )
    target_derived = derive_analysis_snapshot(
        snapshot=source,
        artifact_store=store,
        features=[DerivedFeature(name="proxy", operation="multiply", columns=["y", "x"])],
    )
    target_derived = derive_analysis_snapshot(
        snapshot=target_derived,
        artifact_store=store,
        features=[DerivedFeature(name="proxy2", operation="add", columns=["proxy", "x"])],
    )
    with pytest.raises(ValueError, match="target leakage"):
        promote_analysis_snapshot(
            snapshot=target_derived, target="y", features=["proxy2"], artifact_store=store
        )

    grouped = derive_analysis_snapshot(
        snapshot=source,
        artifact_store=store,
        features=[
            DerivedFeature(name="gmean", operation="group_mean", columns=["x"], group_by=["g"])
        ],
    )
    with pytest.raises(ValueError, match="not predictive safe"):
        promote_analysis_snapshot(
            snapshot=grouped, target="y", features=["gmean"], artifact_store=store
        )


def test_clock_parts_and_public_normality_label():
    rows = [
        {"clock": value} for value in ["7:13 PM", "12:01 AM", "23:45:02", "2026-10-22T14:00:00"]
    ]
    result = derive_features(rows, [DerivedFeature(name="h", operation="hour", columns=["clock"])])
    assert [r["h"] for r in result] == [19, 0, 23, 14]
    request = StatisticsRequest.model_validate(
        {
            "columns": ["x"],
            "operation": "normality",
            "significance_rule": {
                "alpha": 0.05,
                "significant_label": "Not normal",
                "nonsignificant_label": "Normal",
            },
        }
    )
    text = 'alpha=0.05; labels "Not normal" and "Normal"'
    output = compute_statistics([{"x": i} for i in range(10)], request, public_text=text)
    normal = output["groups"][0]["statistics"]["x"]["normality"]
    assert normal["public_rule_label"] == "Normal"
    with pytest.raises(ValueError, match="public text"):
        compute_statistics([{"x": i} for i in range(10)], request)
    insufficient = compute_statistics([{"x": 1}, {"x": 2}], request, public_text=text)
    assert "public_rule_label" not in insufficient["groups"][0]["statistics"]["x"]["normality"]


def test_directional_policy_uses_signed_r_and_rejects_unspecified_weak_class():
    request = StatisticsRequest.model_validate(
        {
            "columns": ["x", "y"],
            "operation": "pearson",
            "direction_rule": {
                "alpha": 0.05,
                "r_threshold": 0.5,
                "positive_label": "positive",
                "negative_label": "negative",
                "nonsignificant_label": "none",
            },
        }
    )
    text = 'alpha=0.05 threshold 0.5; labels "positive", "negative", "none"'
    output = compute_statistics([{"x": i, "y": -i} for i in range(10)], request, public_text=text)
    assert output["groups"][0]["public_rule_label"] == "negative"
    with pytest.raises(ValueError, match="public text"):
        compute_statistics([{"x": i, "y": i} for i in range(10)], request)


def test_numeric_text_coercion_is_explicit_and_keeps_group_keys():
    rows = [
        {"x": "1", "y": "4", "group": "001"},
        {"x": "2", "y": "null", "group": "001"},
        {"x": "3", "y": "2", "group": "001"},
    ]
    with pytest.raises(ValueError, match="nonconstant"):
        compute_statistics(rows, StatisticsRequest(columns=["x", "y"], operation="pearson"))
    request = StatisticsRequest(
        columns=["x", "y"],
        operation="pearson",
        group_by=["group"],
        method=MethodSpec(numeric_coercion="parse"),
    )
    output = compute_statistics(rows, request)
    group = output["groups"][0]
    assert group["correlation"] == pytest.approx(-1)
    assert group["dropped_rows"] == 1 and group["group"] == {"group": "001"}
    assert rows[0]["x"] == "1"
