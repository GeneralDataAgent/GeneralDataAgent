"""Atomic task budgets survive retries, concurrency and process restarts."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from generaldataagent.contracts import ActorContext, Budget


class BudgetExceededError(PermissionError):
    pass


class TaskBudgetLedger:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.clock = path, clock or (lambda: datetime.now(UTC))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS task_budgets ("
                "task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, budget TEXT NOT NULL, "
                "started REAL NOT NULL, max_cost INTEGER NOT NULL, "
                "calls INTEGER NOT NULL, tokens INTEGER NOT NULL, "
                "scan INTEGER NOT NULL, cost INTEGER NOT NULL)"
            )

    @staticmethod
    def _scope(actor: ActorContext) -> str:
        value = actor.model_dump(mode="json")
        value["data_domains"] = sorted(set(value["data_domains"]))
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def register(
        self,
        task_id: UUID,
        actor: ActorContext,
        budget: Budget,
        *,
        max_cost_microunits: int = 1000000,
    ) -> None:
        if type(max_cost_microunits) is not int or max_cost_microunits < 0:
            raise ValueError("invalid cost budget")
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("budget clock must be timezone-aware")
        scope, value = self._scope(actor), budget.model_dump_json()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scope, budget, max_cost FROM task_budgets WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            if row is not None:
                if row != (scope, value, max_cost_microunits):
                    raise BudgetExceededError("task_budget_registration_conflict")
                return  # Never reset usage/deadline on retries.
            db.execute(
                "INSERT INTO task_budgets VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0)",
                (str(task_id), scope, value, now.timestamp(), max_cost_microunits),
            )

    def reserve(
        self,
        task_id: UUID,
        actor: ActorContext,
        *,
        tokens: int = 0,
        scan_bytes: int = 0,
        cost_microunits: int = 0,
    ) -> None:
        if any(
            type(value) is not int or value < 0 for value in (tokens, scan_bytes, cost_microunits)
        ):
            raise ValueError("usage reservations must be nonnegative integers")
        now = self.clock()
        if now.tzinfo is None:
            raise BudgetExceededError("invalid_budget_clock")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scope, budget, started, max_cost, calls, tokens, scan, cost "
                "FROM task_budgets WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            if row is None or row[0] != self._scope(actor):
                raise BudgetExceededError("task_budget_access_denied")
            budget = Budget.model_validate_json(row[1])
            if not 0 <= now.timestamp() - row[2] < budget.max_runtime_seconds:
                raise BudgetExceededError("task_runtime_budget_exceeded")
            usage = (row[4] + 1, row[5] + tokens, row[6] + scan_bytes, row[7] + cost_microunits)
            limits = (budget.max_tool_calls, budget.max_tokens, budget.max_scan_bytes, row[3])
            if any(value > limit for value, limit in zip(usage, limits, strict=True)):
                raise BudgetExceededError("task_usage_budget_exceeded")
            db.execute(
                "UPDATE task_budgets SET calls = ?, tokens = ?, scan = ?, cost = ? "
                "WHERE task_id = ?",
                (*usage, str(task_id)),
            )
