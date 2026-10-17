"""Certified metric analysis wired through the phase-one LangGraph."""

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    AuditEvent,
    ChartSpec,
    DeliveryPackage,
    GroundedClaim,
    MetricCandidate,
    MetricQuery,
    MetricResolution,
    NaturalLanguageMetricQuery,
    TableSpec,
    ToolRequest,
    ValidationCheck,
    ValidationResult,
    ValidationStatus,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.alerts import CriticalAlertRouter
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.orchestration.state import PhaseOneState
from generaldataagent.orchestration.workflow import PhaseOneServices, build_phase_one_graph
from generaldataagent.policy import PolicyEngine
from generaldataagent.security.source import ScopedQueryExecutor
from generaldataagent.tools import SqlGuard, SQLiteReadOnlyExecutor, ToolGateway


class NarrativeGenerator(Protocol):
    def generate(self, evidence: Mapping[str, Any]) -> Any: ...


class MetricAnalysisRuntime:
    """Runs certified metric queries with policy, validation, evidence, and checkpointing."""

    def __init__(
        self,
        *,
        catalog: InMemoryMetricCatalog,
        executor: SQLiteReadOnlyExecutor | ScopedQueryExecutor,
        artifacts: LocalArtifactStore,
        policy: PolicyEngine | None = None,
        audit: JsonlAuditSink | VerifiedAuditSink | None = None,
        narrator: NarrativeGenerator | None = None,
        production_mode: bool = False,
        alerts: CriticalAlertRouter | None = None,
    ) -> None:
        if production_mode and (
            not isinstance(executor, ScopedQueryExecutor)
            or not isinstance(artifacts, GovernedArtifactStore)
        ):
            raise ValueError("production analysis requires scoped source and governed artifacts")
        self.production_mode = production_mode
        self.catalog = catalog
        self.executor = executor
        self.artifacts = artifacts
        self.audit = audit
        self.narrator = narrator
        self.sql_guard = SqlGuard(dialect="sqlite")
        self.gateway = ToolGateway(
            policy or PolicyEngine(), audit, production_mode=production_mode, alerts=alerts
        )
        self.gateway.register("sql.query", self._execute_query)
        self.graph = build_phase_one_graph(
            PhaseOneServices(
                discover=self._discover,
                plan=self._plan,
                analyze=self._analyze,
                validate=self._validate,
                deliver=self._deliver,
            ),
            checkpointer=InMemorySaver(),
        )

    def run(
        self,
        query: MetricQuery,
        actor: ActorContext,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(self.artifacts, GovernedArtifactStore):
            bound = self.artifacts.actor
            if (bound.tenant_id, bound.project_id) != (
                actor.tenant_id,
                actor.project_id,
            ) or not set(actor.data_domains) <= set(bound.data_domains):
                raise PermissionError("artifact_store_actor_scope_mismatch")
        task_id = str(uuid4())
        trace_id = str(uuid4())
        state: PhaseOneState = {
            "task_id": task_id,
            "user_id": actor.user_id,
            "tenant_id": actor.tenant_id,
            "project_id": actor.project_id,
            "user_query": f"certified metric {query.metric_name}@{query.metric_version}",
            "metric_query": query.model_dump(mode="json"),
            "actor_context": actor.model_dump(mode="json"),
            "trace_id": trace_id,
            "status": "created",
        }
        self._emit(
            "analysis.started",
            state,
            {"metric": query.metric_name, "version": query.metric_version},
        )
        try:
            result = self.graph.invoke(
                state,
                {"configurable": {"thread_id": thread_id or task_id}},
            )
        except Exception as exc:
            self._emit(
                "analysis.failed",
                state,
                {"error_type": type(exc).__name__},
            )
            raise
        self._emit("analysis.finished", result, {"status": result["status"]})
        return result

    def resolve_question(
        self,
        request: NaturalLanguageMetricQuery,
        actor: ActorContext,
    ) -> MetricResolution:
        matches = self.catalog.search(
            request.question,
            metric_version=request.metric_version,
            data_domains=actor.data_domains,
        )
        if not matches:
            return MetricResolution(
                status="not_found",
                question=request.question,
                clarification_question=(
                    "No authorized certified metric matched. Specify the metric name or business "
                    "definition."
                ),
            )
        top_score = matches[0].score
        top_matches = tuple(item for item in matches if item.score == top_score)
        candidates = tuple(
            MetricCandidate(
                metric_name=item.metric.name,
                metric_version=item.metric.version,
                description=item.metric.description,
                unit=item.metric.unit,
                matched_alias=item.matched_alias,
            )
            for item in top_matches
        )
        if len(candidates) > 1:
            choices = ", ".join(f"{item.metric_name}@{item.metric_version}" for item in candidates)
            return MetricResolution(
                status="ambiguous",
                question=request.question,
                candidates=candidates,
                clarification_question=f"Choose one certified metric: {choices}.",
            )
        return MetricResolution(
            status="resolved",
            question=request.question,
            candidates=candidates,
        )

    def analyze_question(
        self,
        request: NaturalLanguageMetricQuery,
        actor: ActorContext,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        resolution = self.resolve_question(request, actor)
        if resolution.status != "resolved":
            return {
                "status": "needs_clarification",
                "resolution": resolution.model_dump(mode="json"),
            }
        selected = resolution.candidates[0]
        result = self.run(
            MetricQuery(
                metric_name=selected.metric_name,
                metric_version=selected.metric_version,
                dimensions=request.dimensions,
                start_time=request.start_time,
                end_time=request.end_time,
            ),
            actor,
            thread_id=thread_id,
        )
        return {**result, "resolution": resolution.model_dump(mode="json")}

    def _discover(self, state: PhaseOneState) -> dict[str, Any]:
        request = MetricQuery.model_validate(state["metric_query"])
        actor = ActorContext.model_validate(state["actor_context"])
        metric = self.catalog.get(request.metric_name, request.metric_version)
        if metric.data_domain not in actor.data_domains:
            raise PermissionError(
                f"actor is not authorized for metric data domain: {metric.data_domain}"
            )
        invalid = set(request.dimensions) - set(metric.allowed_dimensions)
        if invalid:
            raise ValueError(f"dimensions are not certified for this metric: {sorted(invalid)}")
        if request.start_time is not None and metric.time_column is None:
            raise ValueError("this metric does not define a certified time column")
        return {"semantic_context": metric.model_dump(mode="json")}

    def _plan(self, state: PhaseOneState) -> dict[str, Any]:
        query = MetricQuery.model_validate(state["metric_query"])
        metric = MetricDefinition.model_validate(state["semantic_context"])
        sql, parameters = self._compile_metric_sql(metric, query)
        inspection = self.sql_guard.inspect(sql, allowed_relations={metric.relation})
        return {
            "task_plan": {
                "metric": metric.qualified_name(),
                "sql": inspection.normalized_sql,
                "parameters": parameters,
                "allowed_relations": sorted(inspection.relations),
                "read_only": True,
            }
        }

    def _analyze(self, state: PhaseOneState) -> dict[str, Any]:
        plan = state["task_plan"]
        actor = ActorContext.model_validate(state["actor_context"])
        request = ToolRequest(
            task_id=state["task_id"],
            user_id=actor.user_id,
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            agent_role=AgentRole.DATA_ANALYST,
            tool_name="sql.query",
            action="query",
            resource=plan["allowed_relations"][0],
            arguments={
                "sql": plan["sql"],
                "actor_context": actor.model_dump(mode="json"),
                "parameters": plan["parameters"],
                "allowed_relations": plan["allowed_relations"],
            },
            data_domains=actor.data_domains,
        )
        result = self.gateway.invoke(request, trace_id=state["trace_id"])
        payload = result.as_dict()
        artifact = self.artifacts.put_json(
            task_id=request.task_id,
            artifact_type="metric_query_result",
            value=payload,
            created_by="data-analyst",
        )
        analysis = {
            **payload,
            "artifact": artifact.model_dump(mode="json"),
            "metric": plan["metric"],
        }
        table = TableSpec(
            columns=tuple(payload["columns"]),
            rows=tuple(tuple(row) for row in payload["rows"]),
            data_artifact_uri=artifact.uri,
        )
        chart = self._build_chart_spec(plan["metric"], table)
        chart_artifact = self.artifacts.put_json(
            task_id=request.task_id,
            artifact_type="chart_spec",
            value=chart.model_dump(mode="json"),
            created_by="deterministic-reporter",
            lineage=(artifact.artifact_id,),
        )
        analysis["table"] = table.model_dump(mode="json")
        analysis["chart"] = chart.model_dump(mode="json")
        analysis["chart_artifact"] = chart_artifact.model_dump(mode="json")
        if self.narrator is not None:
            narrative = self.narrator.generate(
                {
                    "metric": plan["metric"],
                    "columns": payload["columns"],
                    "rows": payload["rows"],
                    "row_count": payload["row_count"],
                }
            )
            narrative_payload = narrative.model_dump(mode="json")
            narrative_artifact = self.artifacts.put_json(
                task_id=request.task_id,
                artifact_type="llm_narrative",
                value=narrative_payload,
                created_by="llm-narrator",
            )
            analysis["narrative"] = narrative_payload
            analysis["narrative_artifact"] = narrative_artifact.model_dump(mode="json")
        analysis["claims"] = [item.model_dump(mode="json") for item in self._build_claims(analysis)]
        return {"analysis_result": analysis}

    def _validate(self, state: PhaseOneState) -> dict[str, Any]:
        analysis = state["analysis_result"]
        checks = (
            ValidationCheck(
                check_id="result-columns",
                passed="metric_value" in analysis["columns"],
                code="metric_value_present",
                message="The result must expose a metric_value column.",
            ),
            ValidationCheck(
                check_id="result-evidence",
                passed=bool(analysis.get("artifact", {}).get("uri")),
                code="artifact_present",
                message="The result must be backed by an immutable artifact reference.",
            ),
            ValidationCheck(
                check_id="narrative-evidence",
                passed=(
                    "narrative" not in analysis
                    or bool(analysis.get("narrative_artifact", {}).get("uri"))
                ),
                code="narrative_artifact_present",
                message="A model-generated narrative must be preserved as an artifact.",
            ),
            ValidationCheck(
                check_id="chart-evidence",
                passed=(
                    bool(analysis.get("chart", {}).get("data_artifact_uri"))
                    and bool(analysis.get("chart_artifact", {}).get("uri"))
                ),
                code="chart_artifact_present",
                message="The chart spec must reference data and be preserved as an artifact.",
            ),
            ValidationCheck(
                check_id="claim-grounding",
                passed=self._claims_are_grounded(analysis),
                code="all_claims_have_evidence",
                message="Every delivery claim must cite evidence produced by this task.",
            ),
        )
        status = (
            ValidationStatus.PASS if all(item.passed for item in checks) else ValidationStatus.FAIL
        )
        validation = ValidationResult(
            task_id=state["task_id"],
            status=status,
            checks=checks,
            verifier="deterministic-metric-verifier",
        )
        return {"validation_result": validation.model_dump(mode="json")}

    @staticmethod
    def _deliver(state: PhaseOneState) -> dict[str, Any]:
        result = state["analysis_result"]
        narrative = result.get("narrative")
        evidence = [result["artifact"], result["chart_artifact"]]
        if result.get("narrative_artifact"):
            evidence.append(result["narrative_artifact"])
        package = DeliveryPackage(
            metric=result["metric"],
            columns=tuple(result["columns"]),
            rows=tuple(tuple(row) for row in result["rows"]),
            row_count=result["row_count"],
            table=TableSpec.model_validate(result["table"]),
            chart=ChartSpec.model_validate(result["chart"]),
            summary=(
                narrative["summary"]
                if narrative
                else f"Certified metric query returned {result['row_count']} row(s)."
            ),
            insights=tuple(narrative["insights"] if narrative else ()),
            limitations=tuple(narrative["limitations"] if narrative else ()),
            claims=tuple(GroundedClaim.model_validate(item) for item in result["claims"]),
            narrative_model=narrative.get("model") if narrative else None,
            evidence=tuple(evidence),
        )
        return {
            "status": "completed",
            "delivery_package": package.model_dump(mode="json"),
        }

    @staticmethod
    def _build_chart_spec(metric: str, table: TableSpec) -> ChartSpec:
        dimensions = [column for column in table.columns if column != "metric_value"]
        if not dimensions:
            chart_type = "kpi"
            x_field = None
        elif len(dimensions) == 1:
            chart_type = "bar"
            x_field = dimensions[0]
        else:
            chart_type = "table"
            x_field = dimensions[0]
        return ChartSpec(
            chart_type=chart_type,
            title=metric,
            x_field=x_field,
            data_artifact_uri=table.data_artifact_uri,
        )

    @staticmethod
    def _build_claims(analysis: dict[str, Any]) -> tuple[GroundedClaim, ...]:
        data_uri = analysis["artifact"]["uri"]
        narrative = analysis.get("narrative")
        if narrative is None:
            return (
                GroundedClaim(
                    claim_id="query-result",
                    claim_type="fact",
                    text=f"Certified metric query returned {analysis['row_count']} row(s).",
                    evidence_uris=(data_uri,),
                ),
            )

        narrative_uri = analysis["narrative_artifact"]["uri"]
        evidence = (data_uri, narrative_uri)
        claims = [
            GroundedClaim(
                claim_id="summary",
                claim_type="interpretation",
                text=narrative["summary"],
                evidence_uris=evidence,
            )
        ]
        claims.extend(
            GroundedClaim(
                claim_id=f"insight-{index}",
                claim_type="interpretation",
                text=text,
                evidence_uris=evidence,
            )
            for index, text in enumerate(narrative["insights"], start=1)
        )
        claims.extend(
            GroundedClaim(
                claim_id=f"limitation-{index}",
                claim_type="limitation",
                text=text,
                evidence_uris=evidence,
            )
            for index, text in enumerate(narrative["limitations"], start=1)
        )
        return tuple(claims)

    @staticmethod
    def _claims_are_grounded(analysis: dict[str, Any]) -> bool:
        evidence_uris = {
            item["uri"]
            for key in ("artifact", "chart_artifact", "narrative_artifact")
            if (item := analysis.get(key)) is not None
        }
        claims = analysis.get("claims", [])
        return bool(claims) and all(
            bool(claim.get("evidence_uris")) and set(claim["evidence_uris"]).issubset(evidence_uris)
            for claim in claims
        )

    def _execute_query(self, arguments: dict[str, Any]) -> Any:
        sql = str(arguments["sql"])
        parameters = tuple(arguments.get("parameters", ()))
        allowed_relations = set(arguments.get("allowed_relations", ()))
        self.sql_guard.inspect(sql, allowed_relations=allowed_relations)
        if isinstance(self.executor, ScopedQueryExecutor):
            actor = ActorContext.model_validate(arguments["actor_context"])
            return self.executor.execute_for_actor(sql, parameters, actor)
        return self.executor.execute(sql, parameters)

    @staticmethod
    def _compile_metric_sql(
        metric: MetricDefinition,
        query: MetricQuery,
    ) -> tuple[str, tuple[Any, ...]]:
        dimensions = list(query.dimensions)
        select_parts = [f'"{dimension}"' for dimension in dimensions]
        select_parts.append(f"{metric.expression} AS metric_value")
        sql = f'SELECT {", ".join(select_parts)} FROM "{metric.relation}"'
        parameters: list[Any] = []
        if query.start_time is not None and query.end_time is not None:
            sql += f' WHERE "{metric.time_column}" >= ? AND "{metric.time_column}" < ?'
            parameters.extend((query.start_time.isoformat(), query.end_time.isoformat()))
        if dimensions:
            quoted = ", ".join(f'"{dimension}"' for dimension in dimensions)
            sql += f" GROUP BY {quoted} ORDER BY {quoted}"
        return sql, tuple(parameters)

    def _emit(self, event_type: str, state: PhaseOneState, payload: dict[str, Any]) -> None:
        if self.audit is None:
            return
        self.audit.emit(
            AuditEvent(
                event_type=event_type,
                task_id=state["task_id"],
                actor_id=state["user_id"],
                tenant_id=state["tenant_id"],
                trace_id=state["trace_id"],
                payload=payload,
            )
        )
