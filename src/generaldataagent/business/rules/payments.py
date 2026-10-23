"""Read-only, bounded merchant/month transaction source for rule context.

Merchant/time rule execution needs actual payment rows, which the facet-only
annex snapshot cannot supply. This module is the one admitted way to reach
them: a fixed database opened read-only, an explicit relation allowlist, and
per-request scoping by merchant and natural year. It performs no writes and
exposes no SQL to callers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from generaldataagent.tools import QueryExecutionError, SQLiteReadOnlyExecutor

MAX_CONTEXT_ROWS = 250_000


class PaymentsSourceError(ValueError):
    """Stable failure code for a scoped payment lookup; never echoes row data."""

    CODES = frozenset(
        {
            "payments_relation_denied",
            "payments_merchant_not_unique",
            "payments_month_empty",
            "payments_invalid_row",
        }
    )

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError("unknown payments failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MerchantYear:
    """One merchant record plus every payment observed in one calendar year."""

    merchant: Mapping[str, Any]
    year: int
    rows: tuple[Mapping[str, Any], ...]


def moment_of(row: Mapping[str, Any]) -> date:
    """Resolve a row's calendar date from ``year`` and one-based ``day_of_year``."""
    try:
        year = int(row["year"])
        day = int(row["day_of_year"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaymentsSourceError("payments_invalid_row") from exc
    if day < 1:
        raise PaymentsSourceError("payments_invalid_row")
    try:
        moment = date(year, 1, 1) + timedelta(days=day - 1)
    except (OverflowError, ValueError) as exc:
        raise PaymentsSourceError("payments_invalid_row") from exc
    if moment.year != year:
        raise PaymentsSourceError("payments_invalid_row")
    return moment


class SQLitePaymentsSource:
    """Scoped reads over a fixed read-only SQLite database.

    The content hash covers both relation snapshots so a changed source is
    visible to the workflow binding. It is cached on the database file's stat
    signature, which is sound for a local, quiescent development database and
    is explicitly not a production freshness guarantee.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        allowed_relations: frozenset[str] | set[str] | tuple[str, ...],
        merchant_table: str = "merchant_data",
        payments_table: str = "payments",
        max_rows: int = MAX_CONTEXT_ROWS,
    ) -> None:
        relations = frozenset(allowed_relations)
        for table in (merchant_table, payments_table):
            if table not in relations:
                raise ValueError("payments source requires its relations to be allowed")
        self.database = Path(database)
        self.allowed_relations = relations
        self.merchant_table = merchant_table
        self.payments_table = payments_table
        self.max_rows = max_rows
        self._cached: tuple[tuple[int, int], str] | None = None

    def _signature(self) -> tuple[int, int]:
        stat = self.database.stat()
        total = stat.st_size
        for suffix in ("-wal", "-shm"):
            companion = self.database.with_name(self.database.name + suffix)
            if companion.exists():
                total += companion.stat().st_size
        return stat.st_mtime_ns, total

    def _rows(self, table: str, *, where: str = "", parameters: tuple[Any, ...] = ()) -> list[dict]:
        if table not in self.allowed_relations:
            raise PaymentsSourceError("payments_relation_denied")
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            result = SQLiteReadOnlyExecutor(
                self.database, max_rows=self.max_rows, timeout_seconds=15
            ).execute(f"SELECT * FROM {quoted} {where}", parameters)
        except (OSError, QueryExecutionError) as exc:
            raise PaymentsSourceError("payments_invalid_row") from exc
        return [dict(zip(result.columns, row, strict=True)) for row in result.rows]

    def _merchant_records(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(row["payload"]) for row in self._rows(self.merchant_table)]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PaymentsSourceError("payments_invalid_row") from exc

    def snapshot_hash(self) -> str:
        """Stable content hash over both relation snapshots."""
        signature = self._signature()
        if self._cached is not None and self._cached[0] == signature:
            return self._cached[1]
        payload = json.dumps(
            [self._merchant_records(), self._rows(self.payments_table)],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        self._cached = (signature, digest)
        return digest

    def merchant_year(self, merchant: str, year: int) -> MerchantYear:
        """Return the single merchant record and its rows for ``year``."""
        records = [row for row in self._merchant_records() if row.get("merchant") == merchant]
        if len(records) != 1:
            raise PaymentsSourceError("payments_merchant_not_unique")
        rows = self._rows(
            self.payments_table,
            where='WHERE "merchant" = ? AND "year" = ?',
            parameters=(merchant, year),
        )
        return MerchantYear(merchant=records[0], year=year, rows=tuple(rows))


def scoped_month(source: MerchantYear, month: int) -> tuple[Mapping[str, Any], ...]:
    """Rows in one natural month; empty when the merchant had no activity."""
    rows = tuple(row for row in source.rows if moment_of(row).month == month)
    if not rows:
        raise PaymentsSourceError("payments_month_empty")
    return rows


def transaction_facets(rows) -> tuple[dict[str, Any], ...]:
    """Project payment rows onto the transaction-side rule facets."""
    facets = []
    for row in rows:
        try:
            amount = Decimal(str(row["eur_amount"]))
            is_credit = _boolean(row.get("is_credit"))
            card_scheme = row["card_scheme"]
            aci = row["aci"]
        except (KeyError, InvalidOperation) as exc:
            raise PaymentsSourceError("payments_invalid_row") from exc
        if not amount.is_finite() or amount < 0:
            raise PaymentsSourceError("payments_invalid_row")
        issuing = row.get("issuing_country")
        acquiring = row.get("acquirer_country")
        facets.append(
            {
                "card_scheme": card_scheme,
                "aci": aci,
                "is_credit": is_credit,
                "intracountry": (
                    None if issuing is None or acquiring is None else issuing == acquiring
                ),
                "amount": format(amount, "f"),
            }
        )
    return tuple(facets)


def _boolean(value: object) -> bool:
    if value in (True, 1, "True", "true", "1"):
        return True
    if value in (False, 0, "False", "false", "0"):
        return False
    raise PaymentsSourceError("payments_invalid_row")
