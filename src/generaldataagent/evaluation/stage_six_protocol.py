"""Separate evaluator invariants from the system being compared."""

import hashlib
import inspect
import json
import platform
import sqlite3
from pathlib import Path

from generaldataagent.evaluation.stage_six import (
    DEADLINE_MARGIN_SECONDS,
    DEFAULT_WORKER_SECONDS,
    DEFAULT_WORKER_STEPS,
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
    if max_output_tokens is not None and (
        type(max_output_tokens) is not int or max_output_tokens < 32
    ):
        raise ValueError("invalid per-call output token budget")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
        raise ValueError("invalid total observed token budget")
    if type(seconds) is not int or seconds < 1:
        raise ValueError("invalid declared time budget")
    if type(hard_case_deadline) is not int or hard_case_deadline <= seconds:
        # A deadline at or below the in-task budget would report a budget
        # timeout as a service failure, and the protocol would not describe
        # the execution the reader is asked to trust.
        raise ValueError("hard case deadline must exceed the worker time budget")
    if type(steps) is not int or steps < 1:
        # The declared step ceiling bounds how much work one case may do; it
        # must be the ceiling actually granted, not a fixed placeholder.
        raise ValueError("invalid declared step budget")
    scoring = "\n".join(
        inspect.getsource(fn) for fn in [_number, compare_value, score_answers, equivalent_rows]
    )
    importing = "\n".join(
        inspect.getsource(fn) for fn in [_number, build_csv_database, add_json_tables]
    )
    execution = "\n".join(inspect.getsource(cls) for cls in [SqlGuard, SQLiteReadOnlyExecutor])
    return {
        "version": "stage6-v7",
        "max_output_tokens_per_call": max_output_tokens,
        "python_runtime": platform.python_version(),
        "sqlite_runtime": sqlite3.sqlite_version,
        "dependency_lock_hash": digest(Path(__file__).resolve().parents[3] / "uv.lock"),
        "service_circuit": "terminal_401_402_403_404_or_three_consecutive_provider_failures",
        "scorer": "strict-infi-absolute1e-6-dab-local-1e-6",
        "scorer_source_hash": hashlib.sha256(scoring.encode()).hexdigest(),
        "import_source_hash": hashlib.sha256(importing.encode()).hexdigest(),
        "execution_source_hash": hashlib.sha256(execution.encode()).hexdigest(),
        "steps": steps,
        "seconds": seconds,
        "hard_case_deadline_seconds": hard_case_deadline,
        "tokens": max_tokens,
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
