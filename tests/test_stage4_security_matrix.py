import json
from pathlib import Path

import pytest

from generaldataagent.evaluation.stage_four import run_security_regression
from generaldataagent.tools import SqlGuard, SqlGuardError

DATASET = Path(__file__).parents[1] / "evals/datasets/stage4-security.jsonl"


def test_frozen_security_matrix_and_distinct_acceptance_boundary():
    report = run_security_regression(DATASET)
    assert report["total"] == 91
    assert report["failed"] == 0, [case for case in report["cases"] if not case["passed"]]
    assert report["production_acceptance"] == "incomplete"
    assert len(report["dataset_sha256"]) == 64


def test_matrix_rejects_small_duplicate_and_malformed_fixtures(tmp_path):
    path = tmp_path / "matrix.jsonl"
    path.write_text(DATASET.read_text().splitlines()[0] + "\n")
    with pytest.raises(ValueError):
        run_security_regression(path)
    path.write_text(path.read_text() * 90)
    with pytest.raises(ValueError):
        run_security_regression(path)
    rows = [json.loads(line) for line in DATASET.read_text().splitlines()]
    rows[0]["input"] = {}
    path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="text"):
        run_security_regression(path)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO copy FROM analytics.orders",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT readfile('/etc/passwd')",
        (
            "WITH x AS (SELECT 1) SELECT * FROM "
            "(WITH secret AS (SELECT 1) SELECT * FROM secret) a, secret"
        ),
        'WITH "restricted.payroll" AS (SELECT 1) SELECT * FROM restricted.payroll',
    ],
)
def test_sql_read_only_scope_and_external_io_hardening(sql):
    with pytest.raises(SqlGuardError):
        SqlGuard().inspect(sql, allowed_relations={"analytics.orders"})
