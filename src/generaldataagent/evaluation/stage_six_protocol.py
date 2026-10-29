"""Separate evaluator invariants from the system being compared."""

import hashlib
import inspect
import json
import platform
import sqlite3
from pathlib import Path

from generaldataagent.evaluation.execution_budget import (
    DEADLINE_MARGIN_SECONDS,
    DEFAULT_WORKER_SECONDS,
    DEFAULT_WORKER_STEPS,
    ExecutionBudget,
)
from generaldataagent.evaluation.stage_six import (
    _number,
    add_json_tables,
    build_csv_database,
    compare_value,
    digest,
    equivalent_rows,
    score_answers,
)
from generaldataagent.tools import SQLiteReadOnlyExecutor
from generaldataagent.tools.sql_guard import SqlGuard


def evaluation_protocol(
    *,
    workers: int,
    max_output_tokens: int | None = None,
    max_tokens: int | None = None,
    hard_case_deadline: int = DEFAULT_WORKER_SECONDS + DEADLINE_MARGIN_SECONDS,
    steps: int = DEFAULT_WORKER_STEPS,
    seconds: int = DEFAULT_WORKER_SECONDS,
) -> dict:
    if type(hard_case_deadline) is not int:
        raise ValueError("hard case deadline must exceed the worker time budget")
    try:
        budget = ExecutionBudget(
            max_steps=steps,
            max_seconds=seconds,
            max_tokens=max_tokens,
            max_output_tokens=max_output_tokens,
            deadline_seconds=hard_case_deadline,
        )
    except ValueError as exc:
        legacy = {
            "invalid worker time budget": "invalid declared time budget",
            "process deadline must exceed the worker time budget": (
                "hard case deadline must exceed the worker time budget"
            ),
        }
        raise ValueError(legacy.get(str(exc), str(exc))) from exc
    scoring = "\n".join(
        inspect.getsource(fn) for fn in [_number, compare_value, score_answers, equivalent_rows]
    )
    importing = "\n".join(
        inspect.getsource(fn) for fn in [_number, build_csv_database, add_json_tables]
    )
    execution = "\n".join(inspect.getsource(cls) for cls in [SqlGuard, SQLiteReadOnlyExecutor])
    return {
        "version": "stage6-v7",
        "max_output_tokens_per_call": budget.max_output_tokens,
        "python_runtime": platform.python_version(),
        "sqlite_runtime": sqlite3.sqlite_version,
        "dependency_lock_hash": digest(Path(__file__).resolve().parents[3] / "uv.lock"),
        "service_circuit": "terminal_401_402_403_404_or_three_consecutive_provider_failures",
        "scorer": "strict-infi-absolute1e-6-dab-local-1e-6",
        "scorer_source_hash": hashlib.sha256(scoring.encode()).hexdigest(),
        "import_source_hash": hashlib.sha256(importing.encode()).hexdigest(),
        "execution_source_hash": hashlib.sha256(execution.encode()).hexdigest(),
        "steps": budget.max_steps,
        "seconds": budget.max_seconds,
        "hard_case_deadline_seconds": budget.process_deadline,
        "tokens": budget.max_tokens,
        "max_cost_usd_per_case": 0.02,
        "call_timeout_seconds": 30,
        "result_row_limit": 200,
        "final_and_crosscheck_row_limit": 10000,
        "final_and_crosscheck_cell_limit": 10000,
        "workers": workers,
        "permitted_effects": "read-only",
        "numeric_import": "finite REAL; empty NULL",
    }


def protocol_hash(protocol: dict) -> str:
    return hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
