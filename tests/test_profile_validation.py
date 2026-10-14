import pytest

from generaldataagent.contracts import ColumnProfile, DataProfile
from generaldataagent.data_engineering import compare_profiles


def profile(*, row_count=3, amount_mean=20.0, include_channel=True) -> DataProfile:
    columns = [
        ColumnProfile(
            name="id",
            data_type="INTEGER",
            row_count=row_count,
            null_count=0,
            distinct_count=row_count,
            minimum=1,
            maximum=row_count,
            mean=2.0,
        ),
        ColumnProfile(
            name="amount",
            data_type="REAL",
            row_count=row_count,
            null_count=0,
            distinct_count=row_count,
            minimum=10.0,
            maximum=30.0,
            mean=amount_mean,
        ),
    ]
    if include_channel:
        columns.append(
            ColumnProfile(
                name="channel",
                data_type="TEXT",
                row_count=row_count,
                null_count=1,
                distinct_count=2,
                minimum="app",
                maximum="web",
            )
        )
    return DataProfile(
        connector_id="oracle",
        relation="orders",
        row_count=row_count,
        columns=tuple(columns),
    )


def test_profile_comparison_passes_exact_oracle() -> None:
    result = compare_profiles(profile(), profile())

    assert result.passed is True
    assert result.agreement_rate == 1.0
    assert result.mismatches == ()


def test_profile_comparison_applies_numeric_tolerance() -> None:
    result = compare_profiles(
        profile(amount_mean=20.0001),
        profile(amount_mean=20.0),
        numeric_tolerance=0.001,
    )

    assert result.passed is True
    assert result.agreement_rate == 1.0


def test_profile_comparison_reports_field_level_mismatch() -> None:
    result = compare_profiles(
        profile(amount_mean=22.0),
        profile(amount_mean=20.0),
        threshold=1.0,
    )

    assert result.passed is False
    assert len(result.mismatches) == 1
    assert result.mismatches[0].column == "amount"
    assert result.mismatches[0].metric == "mean"
    assert result.mismatches[0].expected == 20.0
    assert result.mismatches[0].actual == 22.0


def test_profile_comparison_counts_all_missing_column_statistics() -> None:
    result = compare_profiles(
        profile(include_channel=False),
        profile(),
        threshold=1.0,
    )

    assert result.passed is False
    assert len(result.mismatches) == 6
    assert {item.column for item in result.mismatches} == {"channel"}
    assert {item.metric for item in result.mismatches} == {
        "column_present",
        "null_count",
        "distinct_count",
        "minimum",
        "maximum",
        "mean",
    }


def test_profile_comparison_rejects_different_relations() -> None:
    other = profile().model_copy(update={"relation": "users"})

    with pytest.raises(ValueError, match="same logical relation"):
        compare_profiles(profile(), other)
