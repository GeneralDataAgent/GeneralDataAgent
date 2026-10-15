"""Explicitly enabled, approval-gated SQLite development materialization."""

import re
import sqlite3
import time
from pathlib import Path
from typing import Literal

from generaldataagent.connectors.base import quote_identifier
from generaldataagent.contracts.data_engineering import (
    DevelopmentWriteReport,
    TransformationProposal,
)
from generaldataagent.data_engineering.transformation import build_transformation_proposal

_SAFE_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class DevelopmentWriteError(RuntimeError):
    pass


class SQLiteDevelopmentWriteRunner:
    """Materializes a validated proposal only in one pinned non-production database."""

    def __init__(
        self,
        database_path: Path,
        *,
        connector_id: str,
        environment: Literal["development", "ci"],
        target_prefix: str = "dev_",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.database_path = database_path.resolve(strict=True)
        if not self.database_path.is_file():
            raise ValueError("development database path must be a file")
        if not connector_id:
            raise ValueError("connector_id must not be empty")
        if environment not in {"development", "ci"}:
            raise ValueError("development write environment must be development or ci")
        if not target_prefix or not _SAFE_TARGET.fullmatch(target_prefix):
            raise ValueError("target_prefix must be a safe SQLite identifier prefix")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.connector_id = connector_id
        self.environment = environment
        self.target_prefix = target_prefix
        self.timeout_seconds = timeout_seconds

    def validate(self, proposal: TransformationProposal, *, require_absent: bool = True) -> None:
        if proposal.connector_id != self.connector_id:
            raise ValueError("development proposal connector does not match the pinned runner")
        if proposal.dialect.casefold() != "sqlite":
            raise ValueError("development write runner accepts only SQLite proposals")
        if not _SAFE_TARGET.fullmatch(proposal.target_relation):
            raise ValueError("development target must be one unqualified safe identifier")
        if not proposal.target_relation.startswith(self.target_prefix):
            raise ValueError(
                f"development target must start with the configured prefix: {self.target_prefix}"
            )
        if proposal.target_relation.casefold() in {
            dependency.casefold() for dependency in proposal.dependencies
        }:
            raise ValueError("development target cannot also be a source dependency")
        rebuilt = build_transformation_proposal(
            connector_id=proposal.connector_id,
            dialect=proposal.dialect,
            target_relation=proposal.target_relation,
            select_sql=proposal.select_sql,
            allowed_relations=set(proposal.dependencies),
            tests=proposal.tests,
            impact_summary=proposal.impact_summary,
            rollback_plan=proposal.rollback_plan,
        )
        if (
            rebuilt.select_sql != proposal.select_sql
            or rebuilt.dependencies != proposal.dependencies
        ):
            raise ValueError("development proposal does not match reparsed SQL evidence")
        if require_absent and self._target_exists(proposal.target_relation):
            raise DevelopmentWriteError("development target already exists")

    def rollback_plan(self, proposal: TransformationProposal) -> str:
        self.validate(proposal)
        return f"DROP TABLE {quote_identifier(proposal.target_relation)}"

    def materialize(self, proposal: TransformationProposal) -> DevelopmentWriteReport:
        self.validate(proposal)
        target_sql = quote_identifier(proposal.target_relation)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with sqlite3.connect(self.database_path) as connection:
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0,
                    1_000,
                )
                connection.execute(f"CREATE TABLE {target_sql} AS {proposal.select_sql}")
                rows_written = int(
                    connection.execute(f"SELECT COUNT(*) FROM {target_sql}").fetchone()[0]
                )
        except sqlite3.Error as exc:
            if "interrupted" in str(exc).casefold():
                raise DevelopmentWriteError(
                    f"development materialization exceeded {self.timeout_seconds:g} seconds"
                ) from exc
            raise DevelopmentWriteError(f"development materialization failed: {exc}") from exc
        return DevelopmentWriteReport(
            proposal_id=proposal.proposal_id,
            connector_id=proposal.connector_id,
            target_relation=proposal.target_relation,
            environment=self.environment,
            rows_written=rows_written,
            rollback_sql=f"DROP TABLE {target_sql}",
        )

    def _target_exists(self, target_relation: str) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (target_relation,),
            ).fetchone()
        return row is not None
