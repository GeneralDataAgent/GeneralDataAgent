"""Separate evaluator invariants from the system being compared."""

import hashlib
import inspect
import json

from generaldataagent.evaluation.stage_six import (
    add_json_tables,
    build_csv_database,
    equivalent_rows,
    score_answers,
)
from generaldataagent.tools import SQLiteReadOnlyExecutor
from generaldataagent.tools.sql_guard import SqlGuard


def evaluation_protocol(*, workers: int) -> dict:
    scoring = "\n".join(inspect.getsource(fn) for fn in [score_answers, equivalent_rows])
    importing = "\n".join(inspect.getsource(fn) for fn in [build_csv_database, add_json_tables])
    execution = "\n".join(inspect.getsource(cls) for cls in [SqlGuard, SQLiteReadOnlyExecutor])
    return {
        "version": "stage6-v4",
        "scorer": "strict-infi-absolute1e-6-dab-local-1e-6",
        "scorer_source_hash": hashlib.sha256(scoring.encode()).hexdigest(),
        "import_source_hash": hashlib.sha256(importing.encode()).hexdigest(),
        "execution_source_hash": hashlib.sha256(execution.encode()).hexdigest(),
        "steps": 6,
        "seconds": 120,
        "hard_case_deadline_seconds": 125,
        "tokens": 24000,
        "max_cost_usd_per_case": 0.02,
        "call_timeout_seconds": 30,
        "result_row_limit": 200,
        "workers": workers,
        "permitted_effects": "read-only",
        "numeric_import": "finite REAL; empty NULL",
    }


def protocol_hash(protocol: dict) -> str:
    return hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
