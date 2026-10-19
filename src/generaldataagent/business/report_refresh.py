"""Refresh certified data and freeze a scoped, immutable report before approval."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid5

from generaldataagent.business.governance import actor_scope
from generaldataagent.contracts import ActorContext, ArtifactRef, DeliveryPackage, MetricQuery
from generaldataagent.contracts.business import ScheduledReport
from generaldataagent.orchestration.metric_analysis import MetricAnalysisRuntime
from generaldataagent.security.dlp import TextDLPScanner


class ReportGenerationBusy(ValueError):
    pass


class MetricReportRefresher:
    """A generating record is never silently replayed after process loss.

    Failed read-only generations may retry, but a generated occurrence is pinned forever.
    Scope/content checks are repeated when reusing its artifact or approving delivery.
    """

    def __init__(self, path: Path, *, analysis: MetricAnalysisRuntime):
        if path.resolve().is_relative_to(analysis.artifacts.root):
            raise ValueError("report metadata must be outside the agent-visible artifact tree")
        self.path, self.analysis = path, analysis
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS refreshed_reports ("
                "id TEXT PRIMARY KEY, scope TEXT, query TEXT, state TEXT, ref TEXT)"
            )

    def prepare(
        self, *, schedule_id: UUID, index: int, query: MetricQuery, actor: ActorContext
    ) -> ArtifactRef:
        key = str(uuid5(schedule_id, f"report:{index}"))
        scope, query_json = actor_scope(actor), query.model_dump_json()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scope,query,state,ref FROM refreshed_reports WHERE id=?", (key,)
            ).fetchone()
            if row:
                if row[:2] != (scope, query_json):
                    raise PermissionError("report_occurrence_scope_or_query_changed")
                if row[2] == "ready":
                    ref = ArtifactRef.model_validate_json(row[3])
                    self.read_artifact(ref, actor)
                    return ref
                if row[2] == "generating":
                    raise ReportGenerationBusy(
                        "report_generation_in_progress_or_requires_reconciliation"
                    )
                db.execute("UPDATE refreshed_reports SET state='generating' WHERE id=?", (key,))
            else:
                db.execute(
                    "INSERT INTO refreshed_reports VALUES(?,?,?,'generating',NULL)",
                    (key, scope, query_json),
                )
        try:
            result = self.analysis.run(query, actor, thread_id=f"report:{key}")
            if (
                result.get("status") != "completed"
                or result.get("validation_result", {}).get("status") != "pass"
            ):
                raise ValueError("report_analysis_did_not_pass")
            delivery = DeliveryPackage.model_validate(result["delivery_package"])
            payload = ScheduledReport(
                schedule_id=schedule_id, index=index, query=query, delivery=delivery
            ).model_dump(mode="json")
            TextDLPScanner().check(json.dumps(payload, ensure_ascii=False).encode())
            ref = self.analysis.artifacts.put_json(
                task_id=UUID(result["task_id"]),
                artifact_type="scheduled_report",
                value=payload,
                created_by="scheduled-reporter",
                lineage=tuple(r.artifact_id for r in delivery.evidence),
            )
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute(
                    "UPDATE refreshed_reports SET state='ready',ref=? "
                    "WHERE id=? AND state='generating'",
                    (ref.model_dump_json(), key),
                )
            return ref
        except Exception:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute(
                    "UPDATE refreshed_reports SET state='failed' WHERE id=? AND state='generating'",
                    (key,),
                )
            raise

    def read_artifact(self, ref: ArtifactRef, actor) -> bytes:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute(
                "SELECT scope,ref FROM refreshed_reports WHERE ref=? AND state='ready'",
                (ref.model_dump_json(),),
            ).fetchone()
        if row is None:
            raise PermissionError("report_artifact_not_registered")
        owner = ActorContext.model_validate_json(row[0])
        if (owner.user_id, owner.tenant_id, owner.project_id) != (
            actor.user_id,
            actor.tenant_id,
            actor.project_id,
        ) or not set(owner.data_domains) <= set(actor.data_domains):
            raise PermissionError("report_artifact_access_denied")
        content = self.analysis.artifacts.read_bytes(ref)
        TextDLPScanner().check_export(content, ref.classification)
        return content
