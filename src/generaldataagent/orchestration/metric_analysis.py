"""Certified metric analysis wired through the phase-one LangGraph."""

from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any, Protocol
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore, require_artifact_scope
from generaldataagent.business.actions import content_hash
from generaldataagent.catalog import InMemoryMetricCatalog, MetricDefinition
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    AuditEvent,
    Budget,
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
from generaldataagent.llm.routed import RoutedNarrator
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.alerts import CriticalAlertRouter
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.orchestration.checkpoints import ProtectedSqliteSaver
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.orchestration.state import PhaseOneState
from generaldataagent.orchestration.workflow import PhaseOneServices, build_phase_one_graph
from generaldataagent.policy import PolicyEngine
from generaldataagent.policy.budgets import TaskBudgetLedger
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
        routed_narrator: RoutedNarrator | None = None,
        production_mode: bool = False,
        alerts: CriticalAlertRouter | None = None,
        budgets: TaskBudgetLedger | None = None,
        checkpointer: ProtectedSqliteSaver | None = None,
        composition: ExecutionComposition | None = None,
    ) -> None:
        if composition is not None:
            if composition.artifacts is not artifacts or (
                audit is not None and composition.audit is not audit
            ):
                raise ValueError("analysis_composition_ports_disagree")
            if composition.budgets is not None and (
                budgets is not None and budgets is not composition.budgets
            ):
                # Two ledgers would meter the same task twice and disagree about
                # what it has spent.
                raise ValueError("analysis_composition_budgets_disagree")
            if production_mode and composition.mode != "product":
                raise ValueError("analysis_production_mode_requires_a_product_composition")
            if composition.budgets is not None and composition.task_id is None:
                # A composed ledger meters registered tasks. Without a composed
                # task every run would mint a new one and register a default
                # budget against it, which is the behaviour the composition
                # exists to prevent.
                raise ValueError("analysis_composition_budgets_require_a_task")
            production_mode = composition.mode == "product"
            budgets = composition.budgets or budgets
            audit = composition.audit
        if narrator is not None and routed_narrator is not None:
            raise ValueError("choose either a legacy or a routed narrator")
        if routed_narrator is not None and (
            budgets is None or routed_narrator.executor.budgets is not budgets
        ):
            raise ValueError("routed narration must share the analysis task budget ledger")
        self.routed_narrator = routed_narrator
        if production_mode and (
            not isinstance(checkpointer, ProtectedSqliteSaver)
            or budgets is None
            or narrator is not None
            or not isinstance(executor, ScopedQueryExecutor)
            or not isinstance(artifacts, GovernedArtifactStore)
        ):
            raise ValueError(
                "production analysis requires scoped source, governed artifacts and budgets; "
                "narration must be metered"
            )
        self.checkpointer = checkpointer
        self.budgets = budgets
        self.production_mode = production_mode
        self.composition = composition
        self.catalog = catalog
        self.executor = executor
        self.artifacts = artifacts
        self.audit = audit
        self.narrator = narrator
        self.sql_guard = SqlGuard(dialect="sqlite")
        self.gateway = ToolGateway(
            policy or PolicyEngine(),
            audit,
            production_mode=production_mode,
            alerts=alerts,
            budgets=budgets,
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
            checkpointer=checkpointer or InMemorySaver(),
        )

    def run(
        self,
        query: MetricQuery,
        actor: ActorContext,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        require_artifact_scope(self.artifacts, actor)
        composed = None
        if self.composition is not None:
            identity = self.composition.identity
            identity.require_equal_actor(actor, "analysis_composition_actor_mismatch")
            composed = identity.task_id
        task_id = str(composed or uuid4())
        if self.budgets is not None:
            if composed is not None:
                # A composed task owns its budget. An unregistered or expired
                # task is refused here rather than handed a fresh default that
                # would silently cover usage the operator never authorized.
                self.budgets.ensure_active(composed, actor)
            else:
                self.budgets.register(UUID(task_id), actor, Budget())
        actual_thread_id = thread_id or task_id
        if self.checkpointer is not None:
            self.checkpointer.register_thread(actual_thread_id, actor)
        trace_id = (
            self.composition.identity.resolve_trace(str(uuid4()))
            if self.composition is not None
            else str(uuid4())
        )
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
            execution = (
                self.checkpointer.execution(actual_thread_id, actor)
                if self.checkpointer is not None
                else nullcontext()
            )
            with execution:
                result = self._invoke_graph(
                    state,
                    {"configurable": {"thread_id": actual_thread_id}},
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

    def _invoke_graph(self, state, config):
        # Production raw state/results never enter an environment-enabled external tracer.
        context = tracing_context(enabled=False) if self.production_mode else nullcontext()
        with context:
            return self.graph.invoke(state, config)

    def resume(self, thread_id: str, actor: ActorContext) -> dict[str, Any]:
        if self.checkpointer is None:
            raise PermissionError("durable_checkpoint_not_configured")
        with self.checkpointer.execution(thread_id, actor) as original_actor:
            actor = original_actor
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = self.graph.get_state(config)
            if (
                not snapshot.values
                or ActorContext.model_validate(snapshot.values["actor_context"]) != actor
            ):
                raise PermissionError("checkpoint_access_denied")
            # The checkpointer bound the *thread* to the caller. A composed
            # runtime must also be the principal the composition names, or it
            # would finish another actor's work and write it through this
            # composition's ports and audit sink.
            if self.composition is not None:
                identity = self.composition.identity
                identity.require_equal_actor(actor, "analysis_composition_actor_mismatch")
                identity.resolve_task(
                    UUID(snapshot.values["task_id"]), code="analysis_composition_task_mismatch"
                )
            if self.budgets is not None:
                self.budgets.ensure_active(UUID(snapshot.values["task_id"]), actor)
            if not snapshot.next:
                return dict(snapshot.values)
            return self._invoke_graph(None, config)

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
        if self.narrator is not None or self.routed_narrator is not None:
            narrative_evidence = {
                "metric": plan["metric"],
                "columns": payload["columns"],
                "rows": payload["rows"],
                "row_count": payload["row_count"],
                "query": state["metric_query"],
            }
            if self.routed_narrator is not None:
                narrative, usage = self.routed_narrator.generate(
                    narrative_evidence,
                    task_id=request.task_id,
                    actor=actor,
                    source_hash=content_hash(
                        {
                            "semantic_context": state["semantic_context"],
                            "evidence": narrative_evidence,
                        }
                    ),
                )
                analysis["model_usage"] = usage
                self._emit("analysis.model_usage", state, usage)
            else:
                narrative = self.narrator.generate(narrative_evidence)
            narrative_payload = narrative.model_dump(mode="json")
            narrative_artifact = self.artifacts.put_json(
                task_id=request.task_id,
                artifact_type="llm_narrative",
                value=narrative_payload,
                created_by="llm-narrator",
                lineage=(artifact.artifact_id,),
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
