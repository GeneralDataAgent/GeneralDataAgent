"""Policy-gated runtime for Stage 2 deterministic engineering operations."""

from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import ReadOnlyConnector
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    ApprovalRequest,
    ArtifactClassification,
    RiskLevel,
    ToolRequest,
)
from generaldataagent.contracts.data_engineering import (
    DataProfile,
    DbtCompileReport,
    DbtTestReport,
    DevelopmentWriteReport,
    ImpactAnalysis,
    LineageAsset,
    LineageSnapshot,
    ProfileBatchItem,
    ProfileBatchRequest,
    ProfileBatchResult,
    QualityReport,
    QualityRule,
    QualityRuleProposal,
    RelationSchema,
    SchemaDiff,
    TransformationProposal,
    TransformationTest,
)
from generaldataagent.data_engineering.catalog_lineage import DbtManifestLineageLoader
from generaldataagent.data_engineering.dbt import DbtCompileRunner
from generaldataagent.data_engineering.development_write import SQLiteDevelopmentWriteRunner
from generaldataagent.data_engineering.impact import analyze_impact
from generaldataagent.data_engineering.profile import profile_relation
from generaldataagent.data_engineering.quality import execute_quality_rules, propose_quality_rules
from generaldataagent.data_engineering.schema_diff import compare_schemas
from generaldataagent.data_engineering.schema_gate import (
    SchemaDriftError,
    require_compatible_schema,
)
from generaldataagent.data_engineering.transformation import build_transformation_proposal
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy import ApprovalStore, PolicyEngine
from generaldataagent.tools import ToolGateway


class DataEngineeringRuntime:
    """Keeps connector selection and deterministic work outside the agent context."""

    def __init__(
        self,
        *,
        connectors: Mapping[str, ReadOnlyConnector],
        connector_domains: Mapping[str, frozenset[str]],
        artifacts: LocalArtifactStore,
        dbt_runner: DbtCompileRunner | None = None,
        development_write_runner: SQLiteDevelopmentWriteRunner | None = None,
        lineage_loader: DbtManifestLineageLoader | None = None,
        policy: PolicyEngine | None = None,
        audit: JsonlAuditSink | None = None,
        approvals: ApprovalStore | None = None,
    ) -> None:
        self.connectors = dict(connectors)
        self.connector_domains = dict(connector_domains)
        if not self.connectors:
            raise ValueError("at least one connector is required")
        if self.connectors.keys() != self.connector_domains.keys():
            raise ValueError("every connector must have one connector_domains entry")
        for connector_id, connector in self.connectors.items():
            if connector_id != connector.capabilities.connector_id:
                raise ValueError("connector registry key must match its declared connector_id")
            if not connector.capabilities.read_only:
                raise ValueError("Stage 2 foundation accepts only read-only connectors")
            if not self.connector_domains[connector_id]:
                raise ValueError("every connector must be bound to at least one data domain")
        self.artifacts = artifacts
        self.dbt_runner = dbt_runner
        self.development_write_runner = development_write_runner
        if (
            self.development_write_runner is not None
            and self.development_write_runner.connector_id not in self.connectors
        ):
            raise ValueError("development write runner must reference a registered connector")
        self.lineage_loader = lineage_loader
        self.approvals = approvals
        self._cancel_events: dict[str, Event] = {}
        self._cancel_events_lock = Lock()
        self.gateway = ToolGateway(policy or PolicyEngine(), audit, approvals)
        self.gateway.register("connector.inspect", self._inspect)
        self.gateway.register("data.profile", self._profile)
        self.gateway.register("data.quality", self._quality)
        self.gateway.register("data.quality.propose", self._propose_quality_rules)
        self.gateway.register("schema.diff", self._diff)
        self.gateway.register("transformation.propose", self._propose_transformation)
        self.gateway.register("lineage.impact", self._analyze_impact)
        if self.dbt_runner is not None:
            self.gateway.register("dbt.compile", self._compile_dbt)
            self.gateway.register("dbt.test", self._test_dbt)
        if self.development_write_runner is not None:
            self.gateway.register("development.write", self._materialize_development)
        if self.lineage_loader is not None:
            self.gateway.register("lineage.ingest", self._ingest_lineage)

    def inspect_relation(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        relation: str,
        trace_id: str,
    ) -> RelationSchema:
        authorized_domains = self._authorized_domains(actor, connector_id)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="connector.inspect",
            action="inspect",
            resource=f"{connector_id}:{relation}",
            arguments={"connector_id": connector_id, "relation": relation},
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "relation_schema", result, "data-engineer")
        return result

    def profile_relation(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        relation: str,
        trace_id: str,
        cancel_event: Event | None = None,
    ) -> DataProfile:
        authorized_domains = self._authorized_domains(actor, connector_id)
        cancel_token = str(uuid4()) if cancel_event is not None else None
        if cancel_token is not None:
            with self._cancel_events_lock:
                self._cancel_events[cancel_token] = cancel_event
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="data.profile",
            action="profile",
            resource=f"{connector_id}:{relation}",
            arguments={
                "connector_id": connector_id,
                "relation": relation,
                "cancel_token": cancel_token,
            },
            data_domains=authorized_domains,
        )
        try:
            result = self.gateway.invoke(request, trace_id=trace_id)
        finally:
            if cancel_token is not None:
                with self._cancel_events_lock:
                    self._cancel_events.pop(cancel_token, None)
        self._store(task_id, "data_profile", result, "data-engineer")
        return result

    def diff_schemas(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        before: RelationSchema,
        after: RelationSchema,
        trace_id: str,
    ) -> SchemaDiff:
        authorized_domains = self._authorized_domains(
            actor,
            before.connector_id,
            after.connector_id,
        )
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="schema.diff",
            action="compare",
            resource=before.relation,
            arguments={
                "before": before.model_dump(mode="json"),
                "after": after.model_dump(mode="json"),
            },
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "schema_diff", result, "data-engineer")
        return result

    def guard_schema(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        expected: RelationSchema,
        actual: RelationSchema,
        trace_id: str,
    ) -> SchemaDiff:
        authorized_domains = self._authorized_domains(
            actor,
            expected.connector_id,
            actual.connector_id,
        )
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="schema.diff",
            action="guard",
            resource=expected.relation,
            arguments={
                "before": expected.model_dump(mode="json"),
                "after": actual.model_dump(mode="json"),
                "fail_on_breaking": True,
            },
            data_domains=authorized_domains,
        )
        try:
            result = self.gateway.invoke(request, trace_id=trace_id)
        except SchemaDriftError as exc:
            self._store(task_id, "schema_drift", exc.diff, "data-engineer")
            raise
        self._store(task_id, "schema_drift", result, "data-engineer")
        return result

    def check_quality(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        relation: str,
        rules: tuple[QualityRule, ...],
        trace_id: str,
    ) -> QualityReport:
        authorized_domains = self._authorized_domains(actor, connector_id)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="data.quality",
            action="check",
            resource=f"{connector_id}:{relation}",
            arguments={
                "connector_id": connector_id,
                "relation": relation,
                "rules": [rule.model_dump(mode="json") for rule in rules],
            },
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "quality_report", result, "data-engineer")
        return result

    def propose_quality_rules(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        schema: RelationSchema,
        profile: DataProfile,
        trace_id: str,
    ) -> QualityRuleProposal:
        authorized_domains = self._authorized_domains(actor, schema.connector_id)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="data.quality.propose",
            action="propose",
            resource=f"{schema.connector_id}:{schema.relation}",
            arguments={
                "schema": schema.model_dump(mode="json"),
                "profile": profile.model_dump(mode="json"),
            },
            data_domains=authorized_domains,
            requested_risk=RiskLevel.R0,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "quality_rule_proposal", result, "data-engineer")
        return result

    def propose_transformation(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        target_relation: str,
        select_sql: str,
        allowed_relations: set[str],
        tests: tuple[TransformationTest, ...],
        impact_summary: str,
        rollback_plan: str,
        trace_id: str,
    ) -> TransformationProposal:
        authorized_domains = self._authorized_domains(actor, connector_id)
        connector = self._connector(connector_id)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="transformation.propose",
            action="propose",
            resource=f"{connector_id}:{target_relation}",
            arguments={
                "connector_id": connector_id,
                "dialect": connector.capabilities.dialect,
                "target_relation": target_relation,
                "select_sql": select_sql,
                "allowed_relations": sorted(allowed_relations),
                "tests": [test.model_dump(mode="json") for test in tests],
                "impact_summary": impact_summary,
                "rollback_plan": rollback_plan,
            },
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "transformation_proposal", result, "data-engineer")
        return result

    def analyze_impact(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_ids: tuple[str, ...],
        assets: tuple[LineageAsset, ...],
        changed_assets: tuple[str, ...],
        trace_id: str,
    ) -> ImpactAnalysis:
        authorized_domains = self._authorized_domains(actor, *connector_ids)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="lineage.impact",
            action="analyze",
            resource=",".join(changed_assets),
            arguments={
                "assets": [asset.model_dump(mode="json") for asset in assets],
                "changed_assets": list(changed_assets),
            },
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "impact_analysis", result, "data-engineer")
        return result

    def profile_relations_parallel(
        self,
        *,
        actor: ActorContext,
        requests: tuple[ProfileBatchRequest, ...],
        trace_id: str,
        max_workers: int = 8,
        cancel_event: Event | None = None,
    ) -> ProfileBatchResult:
        if not requests:
            raise ValueError("parallel profiling requires at least one request")
        if max_workers < 1 or max_workers > 32:
            raise ValueError("max_workers must be between 1 and 32")
        request_ids = [request.request_id for request in requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("parallel profiling request ids must be unique")

        if cancel_event is not None and cancel_event.is_set():
            items = tuple(
                self._cancelled_profile_item(request, elapsed_ms=0.0) for request in requests
            )
            return ProfileBatchResult(items=items, all_succeeded=False)

        results: dict[UUID, ProfileBatchItem] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers, len(requests))) as executor:
            pending: dict[Future[DataProfile], tuple[ProfileBatchRequest, float]] = {}
            for request in requests:
                submitted_at = monotonic()
                future = executor.submit(
                    self.profile_relation,
                    task_id=request.task_id,
                    actor=actor,
                    connector_id=request.connector_id,
                    relation=request.relation,
                    trace_id=f"{trace_id}:{request.request_id}",
                    cancel_event=cancel_event,
                )
                pending[future] = (request, submitted_at)
            while pending:
                if cancel_event is not None and cancel_event.is_set():
                    for future, (request, submitted_at) in tuple(pending.items()):
                        if future.cancel():
                            results[request.request_id] = self._cancelled_profile_item(
                                request,
                                elapsed_ms=(monotonic() - submitted_at) * 1_000,
                            )
                            del pending[future]
                if not pending:
                    break
                completed, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    request, submitted_at = pending.pop(future)
                    results[request.request_id] = self._completed_profile_item(
                        future,
                        request,
                        elapsed_ms=(monotonic() - submitted_at) * 1_000,
                    )
        items = tuple(results[request.request_id] for request in requests)
        return ProfileBatchResult(
            items=items,
            all_succeeded=all(item.status == "succeeded" for item in items),
        )

    @staticmethod
    def _cancelled_profile_item(
        request: ProfileBatchRequest,
        *,
        elapsed_ms: float,
    ) -> ProfileBatchItem:
        return ProfileBatchItem(
            request_id=request.request_id,
            task_id=request.task_id,
            connector_id=request.connector_id,
            relation=request.relation,
            status="cancelled",
            elapsed_ms=elapsed_ms,
            error_code="cancelled",
            error_message="profiling request was cancelled before execution",
        )

    @staticmethod
    def _completed_profile_item(
        future: Future[DataProfile],
        request: ProfileBatchRequest,
        *,
        elapsed_ms: float,
    ) -> ProfileBatchItem:
        try:
            profile = future.result()
        except Exception as exc:
            error_code = getattr(getattr(exc, "code", None), "value", None)
            status = "cancelled" if error_code == "cancelled" else "failed"
            return ProfileBatchItem(
                request_id=request.request_id,
                task_id=request.task_id,
                connector_id=request.connector_id,
                relation=request.relation,
                status=status,
                elapsed_ms=elapsed_ms,
                error_code=error_code or type(exc).__name__,
                error_message=str(exc),
            )
        return ProfileBatchItem(
            request_id=request.request_id,
            task_id=request.task_id,
            connector_id=request.connector_id,
            relation=request.relation,
            status="succeeded",
            elapsed_ms=elapsed_ms,
            profile=profile,
        )

    def compile_dbt(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        target: str,
        selected_resources: tuple[str, ...] = (),
        trace_id: str,
    ) -> DbtCompileReport:
        if self.dbt_runner is None:
            raise RuntimeError("dbt compile is not configured")
        authorized_domains = self._authorized_domains(actor, connector_id)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="dbt.compile",
            action="compile",
            resource=f"{connector_id}:{self.dbt_runner.project_dir.name}",
            arguments={
                "target": target,
                "selected_resources": list(selected_resources),
            },
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "dbt_compile_report", result, "data-engineer")
        return result

    def request_dbt_test_approval(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        target: str,
        selected_resources: tuple[str, ...],
        impact_scope: str,
        cost_estimate: str,
        rollback_plan: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        if self.approvals is None:
            raise RuntimeError("approval store is not configured")
        request = self._dbt_test_request(
            task_id=task_id,
            actor=actor,
            connector_id=connector_id,
            target=target,
            selected_resources=selected_resources,
        )
        decision = self.gateway.policy.evaluate(request)
        if decision.decision != "require_approval":
            raise RuntimeError("dbt test policy must require approval")
        approval = self.approvals.create_request(
            request,
            impact_scope=impact_scope,
            cost_estimate=cost_estimate,
            classification=ArtifactClassification.INTERNAL,
            rollback_plan=rollback_plan,
            expires_at=expires_at,
        )
        self._store(task_id, "approval_request", approval, "data-engineer")
        return approval

    def test_dbt(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        target: str,
        selected_resources: tuple[str, ...],
        approval_id: UUID | None,
        trace_id: str,
    ) -> DbtTestReport:
        request = self._dbt_test_request(
            task_id=task_id,
            actor=actor,
            connector_id=connector_id,
            target=target,
            selected_resources=selected_resources,
        )
        result = self.gateway.invoke(
            request,
            trace_id=trace_id,
            approval_id=approval_id,
        )
        self._store(task_id, "dbt_test_report", result, "data-engineer")
        return result

    def _dbt_test_request(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        target: str,
        selected_resources: tuple[str, ...],
    ) -> ToolRequest:
        if self.dbt_runner is None:
            raise RuntimeError("dbt test is not configured")
        self.dbt_runner.validate_test_request(
            target=target,
            selected_resources=selected_resources,
        )
        authorized_domains = self._authorized_domains(actor, connector_id)
        return self._request(
            task_id=task_id,
            actor=actor,
            tool_name="dbt.test",
            action="test",
            resource=f"{connector_id}:{self.dbt_runner.project_dir.name}",
            arguments={
                "target": target,
                "selected_resources": list(selected_resources),
            },
            data_domains=authorized_domains,
            requested_risk=RiskLevel.R2,
        )

    def request_development_write_approval(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        proposal: TransformationProposal,
        impact_scope: str,
        cost_estimate: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        if self.approvals is None:
            raise RuntimeError("approval store is not configured")
        request = self._development_write_request(
            task_id=task_id,
            actor=actor,
            proposal=proposal,
            require_absent=True,
        )
        decision = self.gateway.policy.evaluate(request)
        if decision.decision != "require_approval":
            raise RuntimeError("development write policy must require approval")
        if self.development_write_runner is None:
            raise RuntimeError("development write is not configured")
        approval = self.approvals.create_request(
            request,
            impact_scope=impact_scope,
            cost_estimate=cost_estimate,
            classification=ArtifactClassification.INTERNAL,
            rollback_plan=self.development_write_runner.rollback_plan(proposal),
            expires_at=expires_at,
        )
        self._store(task_id, "approval_request", approval, "data-engineer")
        return approval

    def materialize_development(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        proposal: TransformationProposal,
        approval_id: UUID | None,
        trace_id: str,
    ) -> DevelopmentWriteReport:
        request = self._development_write_request(
            task_id=task_id,
            actor=actor,
            proposal=proposal,
            require_absent=False,
        )
        result = self.gateway.invoke(
            request,
            trace_id=trace_id,
            approval_id=approval_id,
        )
        self._store(task_id, "development_write_report", result, "data-engineer")
        return result

    def _development_write_request(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        proposal: TransformationProposal,
        require_absent: bool,
    ) -> ToolRequest:
        if self.development_write_runner is None:
            raise RuntimeError("development write is not configured")
        self.development_write_runner.validate(proposal, require_absent=require_absent)
        authorized_domains = self._authorized_domains(actor, proposal.connector_id)
        return self._request(
            task_id=task_id,
            actor=actor,
            tool_name="development.write",
            action="materialize",
            resource=f"{proposal.connector_id}:{proposal.target_relation}",
            arguments={
                "environment": self.development_write_runner.environment,
                "proposal": proposal.model_dump(mode="json"),
            },
            data_domains=authorized_domains,
            requested_risk=RiskLevel.R2,
        )

    def ingest_dbt_lineage(
        self,
        *,
        task_id: UUID,
        actor: ActorContext,
        connector_id: str,
        trace_id: str,
    ) -> LineageSnapshot:
        if self.lineage_loader is None:
            raise RuntimeError("dbt lineage ingestion is not configured")
        authorized_domains = self._authorized_domains(actor, connector_id)
        request = self._request(
            task_id=task_id,
            actor=actor,
            tool_name="lineage.ingest",
            action="ingest",
            resource=f"{connector_id}:dbt-manifest",
            arguments={},
            data_domains=authorized_domains,
        )
        result = self.gateway.invoke(request, trace_id=trace_id)
        self._store(task_id, "lineage_snapshot", result, "data-engineer")
        return result

    def _connector(self, connector_id: str) -> ReadOnlyConnector:
        try:
            return self.connectors[connector_id]
        except KeyError as exc:
            raise LookupError(f"unknown connector: {connector_id}") from exc

    def _authorized_domains(
        self,
        actor: ActorContext,
        *connector_ids: str,
    ) -> tuple[str, ...]:
        authorized: set[str] = set()
        actor_domains = set(actor.data_domains)
        for connector_id in connector_ids:
            self._connector(connector_id)
            matching = actor_domains & self.connector_domains[connector_id]
            if not matching:
                raise PermissionError(
                    f"actor is not authorized for connector data domains: {connector_id}"
                )
            authorized.update(matching)
        return tuple(sorted(authorized))

    def _inspect(self, arguments: dict[str, Any]) -> RelationSchema:
        return self._connector(str(arguments["connector_id"])).inspect_relation(
            str(arguments["relation"])
        )

    def _profile(self, arguments: dict[str, Any]) -> DataProfile:
        cancel_token = arguments.get("cancel_token")
        cancel_event = None
        if cancel_token is not None:
            with self._cancel_events_lock:
                cancel_event = self._cancel_events.get(str(cancel_token))
            if cancel_event is None:
                raise RuntimeError("profiling cancellation token is no longer active")
        return profile_relation(
            self._connector(str(arguments["connector_id"])),
            str(arguments["relation"]),
            cancel_event=cancel_event,
        )

    def _quality(self, arguments: dict[str, Any]) -> QualityReport:
        return execute_quality_rules(
            self._connector(str(arguments["connector_id"])),
            str(arguments["relation"]),
            tuple(QualityRule.model_validate(item) for item in arguments["rules"]),
        )

    @staticmethod
    def _propose_quality_rules(arguments: dict[str, Any]) -> QualityRuleProposal:
        return propose_quality_rules(
            RelationSchema.model_validate(arguments["schema"]),
            DataProfile.model_validate(arguments["profile"]),
        )

    @staticmethod
    def _propose_transformation(arguments: dict[str, Any]) -> TransformationProposal:
        return build_transformation_proposal(
            connector_id=str(arguments["connector_id"]),
            dialect=str(arguments["dialect"]),
            target_relation=str(arguments["target_relation"]),
            select_sql=str(arguments["select_sql"]),
            allowed_relations=set(arguments["allowed_relations"]),
            tests=tuple(TransformationTest.model_validate(item) for item in arguments["tests"]),
            impact_summary=str(arguments["impact_summary"]),
            rollback_plan=str(arguments["rollback_plan"]),
        )

    @staticmethod
    def _analyze_impact(arguments: dict[str, Any]) -> ImpactAnalysis:
        return analyze_impact(
            tuple(LineageAsset.model_validate(item) for item in arguments["assets"]),
            tuple(str(item) for item in arguments["changed_assets"]),
        )

    def _compile_dbt(self, arguments: dict[str, Any]) -> DbtCompileReport:
        if self.dbt_runner is None:
            raise RuntimeError("dbt compile is not configured")
        return self.dbt_runner.compile(
            target=str(arguments["target"]),
            selected_resources=tuple(str(item) for item in arguments["selected_resources"]),
        )

    def _test_dbt(self, arguments: dict[str, Any]) -> DbtTestReport:
        if self.dbt_runner is None:
            raise RuntimeError("dbt test is not configured")
        return self.dbt_runner._test_selected(
            target=str(arguments["target"]),
            selected_resources=tuple(str(item) for item in arguments["selected_resources"]),
        )

    def _materialize_development(self, arguments: dict[str, Any]) -> DevelopmentWriteReport:
        if self.development_write_runner is None:
            raise RuntimeError("development write is not configured")
        if arguments.get("environment") != self.development_write_runner.environment:
            raise ValueError("development write environment does not match the pinned runner")
        return self.development_write_runner.materialize(
            TransformationProposal.model_validate(arguments["proposal"])
        )

    def _ingest_lineage(self, arguments: dict[str, Any]) -> LineageSnapshot:
        if arguments:
            raise ValueError("dbt lineage ingestion does not accept runtime paths")
        if self.lineage_loader is None:
            raise RuntimeError("dbt lineage ingestion is not configured")
        return self.lineage_loader.load()

    @staticmethod
    def _diff(arguments: dict[str, Any]) -> SchemaDiff:
        before = RelationSchema.model_validate(arguments["before"])
        after = RelationSchema.model_validate(arguments["after"])
        if arguments.get("fail_on_breaking") is True:
            return require_compatible_schema(before, after)
        return compare_schemas(before, after)

    @staticmethod
    def _request(
        *,
        task_id: UUID,
        actor: ActorContext,
        tool_name: str,
        action: str,
        resource: str,
        arguments: dict[str, Any],
        data_domains: tuple[str, ...],
        requested_risk: RiskLevel = RiskLevel.R1,
    ) -> ToolRequest:
        return ToolRequest(
            task_id=task_id,
            user_id=actor.user_id,
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            agent_role=AgentRole.DATA_ENGINEER,
            tool_name=tool_name,
            action=action,
            resource=resource,
            arguments=arguments,
            data_domains=data_domains,
            requested_risk=requested_risk,
        )

    def _store(
        self,
        task_id: UUID,
        artifact_type: str,
        value: (
            ApprovalRequest
            | DataProfile
            | DevelopmentWriteReport
            | DbtCompileReport
            | DbtTestReport
            | ImpactAnalysis
            | LineageSnapshot
            | QualityReport
            | QualityRuleProposal
            | RelationSchema
            | SchemaDiff
            | TransformationProposal
        ),
        created_by: str,
    ) -> None:
        self.artifacts.put_json(
            task_id=task_id,
            artifact_type=artifact_type,
            value=value.model_dump(mode="json"),
            created_by=created_by,
        )
