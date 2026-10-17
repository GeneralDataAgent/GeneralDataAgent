"""Durable idempotent critical-alert delivery with measured notification latency."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class CriticalAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID = Field(default_factory=uuid4)
    kind: Literal[
        "cross_scope",
        "credential_leak",
        "audit_outage",
        "provider_outage",
        "cost_budget",
        "slo_breach",
    ]
    occurred_at: AwareDatetime
    # No prompts, SQL, bearer credentials or user data in notification payloads.


class AlertReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    delivered_at: AwareDatetime


class AlertDeliveryError(RuntimeError):
    pass


class CriticalAlertRouter:
    """Trusted transport must honor event_id as its durable idempotency key.

    The same event can be retried after a transport failure or crash; destination-side
    idempotency prevents duplicate notifications after an ambiguous acknowledgement.
    Real paging endpoints/recipients are operator configuration, not repository defaults.
    """

    def __init__(
        self,
        path: Path,
        *,
        send: Callable[[CriticalAlert], AlertReceipt],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.send = path, send
        self.clock = clock or (lambda: datetime.now(UTC))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS critical_alerts ("
                "event_id TEXT PRIMARY KEY, event_json TEXT NOT NULL, receipt_json TEXT)"
            )

    def deliver(self, alert: CriticalAlert) -> AlertReceipt:
        now = self.clock()
        if now.tzinfo is None or alert.occurred_at > now:
            raise AlertDeliveryError("invalid_alert_time")
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT event_json, receipt_json FROM critical_alerts WHERE event_id = ?",
                (str(alert.event_id),),
            ).fetchone()
            if row is not None:
                if CriticalAlert.model_validate_json(row[0]) != alert:
                    raise AlertDeliveryError("alert_id_conflict")
                if row[1] is not None:
                    return AlertReceipt.model_validate_json(row[1])
            else:
                db.execute(
                    "INSERT INTO critical_alerts VALUES (?, ?, NULL)",
                    (str(alert.event_id), alert.model_dump_json()),
                )
        # Persist intent before the network call. Delivery may be retried with the same key.
        try:
            receipt = AlertReceipt.model_validate(self.send(alert))
            if (
                receipt.event_id != alert.event_id
                or receipt.delivered_at < alert.occurred_at
                or receipt.delivered_at > self.clock()
            ):
                raise ValueError("invalid delivery acknowledgement")
        except Exception:
            raise AlertDeliveryError("critical_alert_delivery_failed") from None
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute(
                "UPDATE critical_alerts SET receipt_json = ? WHERE event_id = ? "
                "AND receipt_json IS NULL",
                (receipt.model_dump_json(), str(alert.event_id)),
            )
            saved = db.execute(
                "SELECT receipt_json FROM critical_alerts WHERE event_id = ?",
                (str(alert.event_id),),
            ).fetchone()
        return AlertReceipt.model_validate_json(saved[0])

    def signal(self, kind: str) -> AlertReceipt:
        now = self.clock()
        event_id = uuid5(NAMESPACE_URL, f"gda-critical:{kind}:{int(now.timestamp()) // 300}")
        alert = CriticalAlert(event_id=event_id, kind=kind, occurred_at=now)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT event_json FROM critical_alerts WHERE event_id = ?", (str(event_id),)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO critical_alerts VALUES (?, ?, NULL)",
                    (str(event_id), alert.model_dump_json()),
                )
            else:
                alert = CriticalAlert.model_validate_json(row[0])
        return self.deliver(alert)

    def retry_pending(
        self, *, limit: int = 10, should_stop: Callable[[], bool] | None = None
    ) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("alert retry batch must be between 1 and 100")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT event_json FROM critical_alerts WHERE receipt_json IS NULL "
                "ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        attempted = delivered = failed = 0
        for (value,) in rows:
            if should_stop is not None and should_stop():
                break
            attempted += 1
            try:
                self.deliver(CriticalAlert.model_validate_json(value))
                delivered += 1
            except (AlertDeliveryError, ValueError):
                failed += 1
        return {"attempted": attempted, "delivered": delivered, "failed": failed}

    def measurements(self) -> dict[str, int | float | None]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT event_json, receipt_json FROM critical_alerts").fetchall()
        latencies = [
            (
                AlertReceipt.model_validate_json(receipt).delivered_at
                - CriticalAlert.model_validate_json(event).occurred_at
            ).total_seconds()
            for event, receipt in rows
            if receipt is not None
        ]
        return {
            "total": len(rows),
            "pending": sum(receipt is None for _, receipt in rows),
            "max_delivery_seconds": max(latencies) if latencies else None,
            "late_deliveries": sum(value > 300 for value in latencies),
        }
