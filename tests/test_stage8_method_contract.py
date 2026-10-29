"""Every method-spec field the eight public diagnostics relied on must do something.

The diagnostics behind `stage8/public-methods-v4` recorded one method spec per
case. Eight fields appear across them; two of those — `iqr_multiplier` and
`z_threshold` — had no regression tying the recorded value to a result. A field
that changes nothing would still be recorded as if it had been applied.
"""

import pytest
from pydantic import ValidationError

from generaldataagent.contracts.statistics import MethodSpec, StatisticsRequest
from generaldataagent.data_science.statistics import compute_statistics

ROWS = [{"x": float(value)} for value in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100)]

# The distinct specs the eight diagnostics recorded: the default one shared by
# six cases, plus the normality and coercion variants.
RECORDED_SPECS = [
    {
        "alpha": 0.05,
        "alternative": "two-sided",
        "ddof": 1,
        "iqr_multiplier": 1.5,
        "missing": "omit",
        "normality": "auto",
        "numeric_coercion": "strict",
        "z_threshold": 3.0,
    },
    {
        "alpha": 0.05,
        "alternative": "two-sided",
        "ddof": 1,
        "iqr_multiplier": 1.5,
        "missing": "omit",
        "normality": "shapiro",
        "numeric_coercion": "strict",
        "z_threshold": 3.0,
    },
    {
        "alpha": 0.05,
        "alternative": "two-sided",
        "ddof": 1,
        "iqr_multiplier": 1.5,
        "missing": "omit",
        "normality": "auto",
        "numeric_coercion": "parse",
        "z_threshold": 3.0,
    },
]


def statistics_of(**method):
    request = StatisticsRequest(columns=["x"], method=MethodSpec(**method))
    return compute_statistics(ROWS, request)["groups"][0]["statistics"]["x"]


def test_every_recorded_method_spec_is_still_expressible():
    for spec in RECORDED_SPECS:
        assert MethodSpec.model_validate(spec).model_dump() == spec


def test_the_iqr_multiplier_moves_the_reported_bounds_and_only_shrinks_the_set():
    counts = []
    for multiplier in (1.5, 3.0, 20.0):
        result = statistics_of(iqr_multiplier=multiplier)
        assert result["iqr_upper"] == pytest.approx(
            result["q3"] + multiplier * result["iqr"], rel=0, abs=1e-9
        )
        counts.append(result["iqr_outlier_count"])
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 1 and counts[-1] == 0


def test_the_z_threshold_moves_the_reported_set():
    assert statistics_of(z_threshold=2.0)["z_outlier_count"] == 1
    assert statistics_of(z_threshold=3.0)["z_outlier_count"] == 0
    assert statistics_of(z_threshold=8.0)["z_outlier_count"] == 0


@pytest.mark.parametrize(
    "method",
    [
        {"iqr_multiplier": 0},
        {"iqr_multiplier": -1.5},
        {"z_threshold": 0},
        {"z_threshold": -3},
    ],
)
def test_a_threshold_that_cannot_discriminate_is_refused(method):
    with pytest.raises(ValidationError):
        MethodSpec(**method)


def test_the_defaults_are_the_values_the_diagnostics_recorded():
    default = MethodSpec().model_dump()
    assert default["iqr_multiplier"] == 1.5
    assert default["z_threshold"] == 3.0
    assert default["alternative"] == "two-sided"
    assert RECORDED_SPECS[0] == default
