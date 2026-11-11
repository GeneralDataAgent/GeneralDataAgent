"""Certified metric analysis wired through the phase-one LangGraph."""

from collections.abc import Callable, Mapping
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
    MetricQuery,
    MetricResolution,
    NaturalLanguageMetricQuery,
    TableSpec,
    ToolRequest,
    ValidationCheck,
    ValidationResult,
    ValidationStatus,
)
from generaldataagent.contracts.routing_snapshot import RoutingSnapshot
from generaldataagent.llm.routed import RoutedNarrator
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.alerts import CriticalAlertRouter
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.orchestration.checkpoints import ProtectedSqliteSaver
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.orchestration.questions import (
    certified_query,
    resolve_question,
    validate_metric_query,
)
from generaldataagent.orchestration.state import PhaseOneState
from generaldataagent.orchestration.task_events import TaskEventJournal, TaskPhase
from generaldataagent.orchestration.workflow import PhaseOneServices, build_phase_one_graph
from generaldataagent.policy import PolicyEngine
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.source import ScopedQueryExecutor
from generaldataagent.tools import SqlGuard, SQLiteReadOnlyExecutor, ToolGateway
from generaldataagent.tools.sqlite_guarded import GuardedSQLiteReadOnlyExecutor


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
        task_events: TaskEventJournal | None = None,
        execution_guard: Callable[[], None] | None = None,
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
        if production_mode and task_events is not None:
            raise ValueError("local_task_events_not_qualified_for_production")
        self.task_events = task_events
        self.execution_guard = execution_guard
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
                discover=self._observed(self._discover, "discover"),
                plan=self._observed(self._plan, "plan"),
                analyze=self._observed(self._analyze, "analyze"),
                validate=self._observed(self._validate, "validate"),
                deliver=self._observed(self._deliver, "deliver"),
            ),
            checkpointer=checkpointer or InMemorySaver(),
        )

    def run(
        self,
        query: MetricQuery,
        actor: ActorContext,
        *,
        thread_id: str | None = None,
        recover_registered: bool = False,
    ) -> dict[str, Any]:
        self._guard_execution()
        if recover_registered and (
            self.composition is None
            or self.composition.task_id is None
            or self.checkpointer is None
            or self.task_events is None
            or self.execution_guard is None
            or thread_id is None
        ):
            raise ValueError("registered_execution_requires_bound_durable_ports")
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
            try:
                self.checkpointer.register_thread(actual_thread_id, actor)
            except PermissionError as exc:
                if not recover_registered or str(exc) != "thread_already_registered_use_resume":
                    raise
                self.checkpointer.authorize_thread(actual_thread_id, actor)
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
        if self.task_events is not None:
            if recover_registered:
                self.task_events.ensure_started(UUID(task_id), actor)
            else:
                self.task_events.start(UUID(task_id), actor)
        self._emit(
            "analysis.started",
            state,
            {"metric": query.metric_name, "version": query.metric_version},
        )
        try:
            execution = (
                self.checkpointer.execution(actual_thread_id, actor, guard=self.execution_guard)
                if self.checkpointer is not None
                else nullcontext()
            )
            with execution:
                config = {"configurable": {"thread_id": actual_thread_id}}
                snapshot = self.graph.get_state(config) if recover_registered else None
                if snapshot is not None and snapshot.values:
                    existing = snapshot.values
                    if (
                        existing.get("task_id") != task_id
                        or existing.get("actor_context") != state["actor_context"]
                        or existing.get("metric_query") != state["metric_query"]
                    ):
                        raise PermissionError("registered_checkpoint_request_mismatch")
                    result = self._invoke_graph(None, config) if snapshot.next else dict(existing)
                else:
                    if recover_registered:
                        cursor = 0
                        while True:
                            page = self.task_events.read(UUID(task_id), actor, after=cursor)
                            if any(event.kind == "phase" for event in page.events):
                                raise ValueError("registered_checkpoint_missing_after_execution")
                            cursor = page.next_cursor
                            if not page.has_more:
                                break
                    result = self._invoke_graph(state, config)
                self._guard_execution()
        except Exception as exc:
            if not recover_registered or not self.task_events.read(UUID(task_id), actor).terminal:
                self._progress(state, "interrupted")
            self._emit(
                "analysis.failed",
                state,
                {"error_type": type(exc).__name__},
            )
            raise
        self._emit("analysis.finished", result, {"status": result["status"]})
        if recover_registered:
            page = self.task_events.read(UUID(task_id), actor)
            if page.terminal:
                last = self.task_events.read(UUID(task_id), actor, after=page.latest_sequence - 1)
                if last.events[0].outcome != result["status"]:
                    raise ValueError("registered_terminal_outcome_mismatch")
            else:
                self._progress(result, "finished", outcome=result["status"])
        else:
            self._progress(result, "finished", outcome=result["status"])
        self._guard_execution()
        return result

    def _guard_execution(self):
        if self.execution_guard is not None:
            self.execution_guard()
            if self.budgets is not None and self.composition is not None:
                self.budgets.ensure_active(self.composition.task_id, self.composition.actor)

    def _observed(self, handler, phase: TaskPhase):
        def observed(state):
            self._guard_execution()
            self._progress(state, "phase", phase=phase)
            result = handler(state)
            self._guard_execution()
            return result

        return observed

    def _progress(self, state, kind, **metadata):
        if self.task_events is not None:
            self.task_events.append(
                UUID(state["task_id"]),
                ActorContext.model_validate(state["actor_context"]),
                kind,
                **metadata,
            )

    def _invoke_graph(self, state, config):
        # Production raw state/results never enter an environment-enabled external tracer.
        context = tracing_context(enabled=False) if self.production_mode else nullcontext()
        with context:
            return self.graph.invoke(state, config)

    def resume(self, thread_id: str, actor: ActorContext) -> dict[str, Any]:
        if self.checkpointer is None:
            raise PermissionError("durable_checkpoint_not_configured")
        with self.checkpointer.execution(
            thread_id, actor, guard=self.execution_guard
        ) as original_actor:
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
                if self.task_events is not None:
                    page = self.task_events.read(UUID(snapshot.values["task_id"]), actor)
                    if not page.terminal:
                        self._progress(
                            snapshot.values, "finished", outcome=snapshot.values["status"]
                        )
                return dict(snapshot.values)
            try:
                result = self._invoke_graph(None, config)
            except Exception:
                self._progress(snapshot.values, "interrupted")
                raise
            self._progress(result, "finished", outcome=result["status"])
            return result

    def resolve_question(
        self,
        request: NaturalLanguageMetricQuery,
        actor: ActorContext,
    ) -> MetricResolution:
        return resolve_question(self.catalog, request, actor)

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
        query, _ = certified_query(self.catalog, resolution, request)
        result = self.run(query, actor, thread_id=thread_id)
        return {**result, "resolution": resolution.model_dump(mode="json")}

    def _discover(self, state: PhaseOneState) -> dict[str, Any]:
        request = MetricQuery.model_validate(state["metric_query"])
        actor = ActorContext.model_validate(state["actor_context"])
        metric = self.catalog.get(request.metric_name, request.metric_version)
        if metric.data_domain not in actor.data_domains:
            raise PermissionError(
                f"actor is not authorized for metric data domain: {metric.data_domain}"
            )
        validate_metric_query(metric, request)
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
        charts = self._build_chart_specs(plan["metric"], table)
        chart_artifacts = tuple(
            self.artifacts.put_json(
                task_id=request.task_id,
                artifact_type="chart_spec",
                value=chart.model_dump(mode="json"),
                created_by="deterministic-reporter",
                lineage=(artifact.artifact_id,),
            )
            for chart in charts
        )
        analysis["table"] = table.model_dump(mode="json")
        analysis["chart"] = charts[0].model_dump(mode="json")
        analysis["chart_artifact"] = chart_artifacts[0].model_dump(mode="json")
        analysis["charts"] = [chart.model_dump(mode="json") for chart in charts]
        analysis["chart_artifacts"] = [item.model_dump(mode="json") for item in chart_artifacts]
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
                snapshot = usage.get("routing_snapshot")
                if snapshot is not None:
                    declaration = RoutingSnapshot.model_validate(snapshot)
                    if declaration.task_id != request.task_id:
                        raise ValueError("routing_snapshot_task_mismatch")
                    snapshot_artifact = self.artifacts.put_json(
                        task_id=request.task_id,
                        artifact_type="routing_snapshot",
                        value=declaration.model_dump(mode="json"),
                        created_by="deterministic-router",
                    )
                    analysis["routing_snapshot"] = declaration.model_dump(mode="json")
                    analysis["routing_snapshot_artifact"] = snapshot_artifact.model_dump(
                        mode="json"
                    )
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
                    bool(analysis.get("charts"))
                    and all(chart.get("data_artifact_uri") for chart in analysis.get("charts", []))
                    and len(analysis.get("chart_artifacts", ())) == len(analysis.get("charts", ()))
                    and all(item.get("uri") for item in analysis.get("chart_artifacts", ()))
                ),
                code="chart_artifact_present",
                message="Every chart spec must reference data and be preserved as an artifact.",
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
        evidence = [result["artifact"], *result["chart_artifacts"]]
        if result.get("narrative_artifact"):
            evidence.append(result["narrative_artifact"])
        if result.get("routing_snapshot_artifact"):
            evidence.append(result["routing_snapshot_artifact"])
        package = DeliveryPackage(
            metric=result["metric"],
            columns=tuple(result["columns"]),
            rows=tuple(tuple(row) for row in result["rows"]),
            row_count=result["row_count"],
            table=TableSpec.model_validate(result["table"]),
            chart=ChartSpec.model_validate(result["chart"]),
            charts=tuple(ChartSpec.model_validate(item) for item in result["charts"]),
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
    def _build_chart_specs(metric: str, table: TableSpec) -> tuple[ChartSpec, ...]:
        """Build the primary visual and one non-aggregating detail chart when possible.

        A dimension chart gets a second table view over the same immutable result artifact. Both
        charts reference the delivery table's data artifact; no chart embeds or recomputes data.
        """
        dimensions = [column for column in table.columns if column != "metric_value"]
        if not dimensions:
            return (
                ChartSpec(
                    chart_type="kpi",
                    title=metric,
                    x_field=None,
                    data_artifact_uri=table.data_artifact_uri,
                ),
            )
        primary_type = "bar" if len(dimensions) == 1 else "table"
        primary = ChartSpec(
            chart_type=primary_type,
            title=metric,
            x_field=dimensions[0],
            data_artifact_uri=table.data_artifact_uri,
        )
        if primary_type == "table" or not table.rows:
            return (primary,)
        return (
            primary,
            ChartSpec(
                chart_type="table",
                title="结果明细",
                x_field=dimensions[0],
                data_artifact_uri=table.data_artifact_uri,
            ),
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
        if isinstance(self.executor, GuardedSQLiteReadOnlyExecutor):
            with self.executor.guarded(self._guard_execution):
                return self.executor.execute(sql, parameters)
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
