"""Policy-gated orchestration for reproducible fixed-template experiments."""

from dataclasses import dataclass

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.artifacts.governed import GovernedArtifactStore, require_artifact_scope
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    AuditEvent,
    DatasetSnapshot,
    ExperimentReport,
    ExperimentSpec,
    MetricValue,
    ModelCard,
    RiskLevel,
    ScienceCheck,
    SubgroupEvaluation,
    ToolRequest,
)
from generaldataagent.data_science.experiment import prepare_experiment, run_prepared_experiment
from generaldataagent.data_science.sandbox import FixedTemplateSandbox, OCIContainerSandbox
from generaldataagent.data_science.snapshot import load_snapshot_rows
from generaldataagent.data_science.tracking import SQLiteExperimentTracker
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.verified_audit import VerifiedAuditSink
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.policy import PolicyEngine


@dataclass(frozen=True, slots=True)
class ScienceGateError(ValueError):
    checks: tuple[ScienceCheck, ...]

    def __str__(self) -> str:
        failed = [check.code for check in self.checks if not check.passed]
        return f"data-science validation failed closed: {failed}"


class DataScienceWorkbench:
    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        policy: PolicyEngine | None = None,
        sandbox: FixedTemplateSandbox | OCIContainerSandbox | None = None,
        audit: JsonlAuditSink | VerifiedAuditSink | None = None,
        tracker: SQLiteExperimentTracker | None = None,
        production_mode: bool = False,
        composition: ExecutionComposition | None = None,
    ) -> None:
        if composition is not None:
            if composition.artifacts is not artifact_store or (
                audit is not None and composition.audit is not audit
            ):
                raise ValueError("science_composition_ports_disagree")
            if production_mode and composition.mode != "product":
                raise ValueError("science_production_mode_requires_a_product_composition")
            production_mode = composition.mode == "product"
            audit = composition.audit
        if production_mode and (
            not isinstance(artifact_store, GovernedArtifactStore)
            or not isinstance(audit, VerifiedAuditSink)
            or not isinstance(sandbox, OCIContainerSandbox)
        ):
            raise ValueError(
                "production science requires governed artifacts, independent audit and OCI sandbox"
            )
        self.production_mode = production_mode
        self.composition = composition
        self.artifact_store = artifact_store
        self.policy = policy or PolicyEngine()
        self.sandbox = sandbox or FixedTemplateSandbox()
        self.audit = audit
        self.tracker = tracker

    def run_experiment(
        self,
        *,
        snapshot: DatasetSnapshot,
        spec: ExperimentSpec,
        user_id: str,
        tenant_id: str,
        project_id: str,
        data_domain: str,
        trace_id: str | None = None,
    ) -> ExperimentReport:
        if self.production_mode and spec.duplicate_policy != "reject":
            raise PermissionError("production science requires duplicate rejection")
        if self.composition is not None:
            bound = self.composition.actor
            if (bound.user_id, bound.tenant_id, bound.project_id) != (
                user_id,
                tenant_id,
                project_id,
            ) or data_domain not in bound.data_domains:
                raise PermissionError("science_composition_actor_mismatch")
            if self.composition.task_id is not None and spec.task_id != self.composition.task_id:
                raise PermissionError("science_composition_task_mismatch")
            if self.composition.budgets is not None:
                # The task owns its budget; an unregistered or expired task is
                # refused here, before any policy decision is recorded.
                self.composition.budgets.ensure_active(spec.task_id, bound)
        require_artifact_scope(
            self.artifact_store,
            ActorContext(
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                data_domains=(data_domain,),
            ),
        )
        if spec.task_id != snapshot.task_id or spec.snapshot_id != snapshot.snapshot_id:
            raise ValueError("experiment must bind the exact task and snapshot")
        if (
            snapshot.tenant_id != tenant_id
            or snapshot.project_id != project_id
            or snapshot.data_domain != data_domain
        ):
            raise PermissionError("experiment actor scope does not match the immutable snapshot")
        self._validate_schema_binding(snapshot, spec)
        request = ToolRequest(
            task_id=spec.task_id,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_role=AgentRole.DATA_SCIENTIST,
            tool_name="python.experiment",
            action="run_fixed_template",
            resource=str(snapshot.snapshot_id),
            arguments={
                "experiment_id": str(spec.experiment_id),
                "snapshot_hash": snapshot.content_hash,
                "template_version": spec.template_version,
            },
            data_domains=(data_domain,),
            requested_risk=RiskLevel.R1,
        )
        decision = self.policy.evaluate(request)
        active_trace_id = (
            self.composition.trace_id
            if self.composition is not None and self.composition.trace_id is not None
            else trace_id or f"experiment-{spec.experiment_id}"
        )
        self._audit(
            event_type="science.policy_decision",
            task_id=spec.task_id,
            actor_id=user_id,
            tenant_id=tenant_id,
            trace_id=active_trace_id,
            payload={
                "request_id": str(request.request_id),
                "decision": decision.decision,
                "reason_code": decision.reason_code,
                "experiment_id": str(spec.experiment_id),
            },
        )
        if decision.decision != "allow":
            raise PermissionError(f"experiment denied by policy: {decision.reason_code}")
        if self.composition is not None and self.composition.budgets is not None:
            self.composition.budgets.reserve(spec.task_id, self.composition.actor)
        rows = load_snapshot_rows(snapshot, self.artifact_store)
        # One shared preparation path: the regression adapter splits and checks
        # the same way, so a control added here reaches both callers.
        prepared = prepare_experiment(rows, spec)
        checks = prepared.checks
        if prepared.blocked:
            self._audit(
                event_type="science.validation_failed",
                task_id=spec.task_id,
                actor_id=user_id,
                tenant_id=tenant_id,
                trace_id=active_trace_id,
                payload={
                    "experiment_id": str(spec.experiment_id),
                    "failed_checks": [check.code for check in checks if not check.passed],
                },
            )
            raise ScienceGateError(checks)
        result = run_prepared_experiment(prepared, sandbox=self.sandbox)
        result_artifact = self.artifact_store.put_json(
            task_id=spec.task_id,
            artifact_type="experiment_result",
            value=result,
            created_by="data_scientist",
            lineage=(snapshot.data_artifact.artifact_id,),
        )
        metrics = tuple(
            MetricValue(name=name, value=float(value))
            for name, value in sorted(result["metrics"].items())
        )
        subgroups = tuple(
            SubgroupEvaluation(
                column=item["column"],
                value=item["value"],
                row_count=item["row_count"],
                metrics=tuple(
                    MetricValue(name=name, value=float(value))
                    for name, value in sorted(item["metrics"].items())
                ),
            )
            for item in result["subgroups"]
        )
        checks = checks + tuple(
            ScienceCheck(
                check_id=f"subgroup_size_{index}",
                passed=subgroup.row_count >= spec.minimum_subgroup_rows,
                severity="warning",
                code=(
                    "subgroup_size_sufficient"
                    if subgroup.row_count >= spec.minimum_subgroup_rows
                    else "subgroup_too_small"
                ),
                message=(
                    f"Subgroup {subgroup.column}={subgroup.value} has "
                    f"{subgroup.row_count} held-out rows."
                ),
            )
            for index, subgroup in enumerate(subgroups)
        )
        report = ExperimentReport(
            experiment_id=spec.experiment_id,
            snapshot_id=snapshot.snapshot_id,
            runtime_fingerprint=self.sandbox.runtime_fingerprint,
            template_version=result["template_version"],
            train_rows=result["train_rows"],
            test_rows=result["test_rows"],
            metrics=metrics,
            eda=result["eda"],
            statistical_checks=result["statistical_checks"],
            error_analysis=result["error_analysis"],
            subgroups=subgroups,
            checks=checks,
            passed=True,
            output_artifact=result_artifact,
        )
        if self.tracker is not None:
            self.tracker.record(spec=spec, report=report)
        self._audit(
            event_type="science.experiment_completed",
            task_id=spec.task_id,
            actor_id=user_id,
            tenant_id=tenant_id,
            trace_id=active_trace_id,
            payload={
                "experiment_id": str(spec.experiment_id),
                "snapshot_id": str(snapshot.snapshot_id),
                "output_artifact_id": str(result_artifact.artifact_id),
                "runtime_fingerprint": report.runtime_fingerprint,
            },
        )
        return report

    @staticmethod
    def build_model_card(
        *,
        snapshot: DatasetSnapshot,
        report: ExperimentReport,
        model_name: str,
        model_version: str,
        intended_use: str,
        owner: str,
        out_of_scope_uses: tuple[str, ...],
        ethical_considerations: tuple[str, ...],
        limitations: tuple[str, ...],
        monitoring_requirements: tuple[str, ...],
        subgroup_evaluation: str,
    ) -> ModelCard:
        if not report.passed or report.snapshot_id != snapshot.snapshot_id:
            raise ValueError("model card requires a passed report for the exact snapshot")
        return ModelCard(
            model_name=model_name,
            model_version=model_version,
            experiment_id=report.experiment_id,
            snapshot_id=snapshot.snapshot_id,
            intended_use=intended_use,
            out_of_scope_uses=out_of_scope_uses,
            training_data=(
                f"Immutable snapshot {snapshot.snapshot_id} from {snapshot.source_name}; "
                f"content {snapshot.content_hash}; {report.train_rows} training rows."
            ),
            evaluation_data=(
                f"Held-out deterministic split with {report.test_rows} rows; "
                f"experiment {report.experiment_id}."
            ),
            subgroup_evaluation=subgroup_evaluation,
            metrics=report.metrics,
            ethical_considerations=ethical_considerations,
            limitations=limitations,
            monitoring_requirements=monitoring_requirements,
            owner=owner,
        )

    @staticmethod
    def _validate_schema_binding(snapshot: DatasetSnapshot, spec: ExperimentSpec) -> None:
        roles = {column.name: column.role for column in snapshot.columns}
        if roles.get(spec.target_column) != "target":
            raise ValueError("experiment target must match the snapshot target role")
        invalid_features = [name for name in spec.feature_columns if roles.get(name) != "feature"]
        if invalid_features:
            raise ValueError(f"experiment features do not match snapshot roles: {invalid_features}")
        invalid_subgroups = [name for name in spec.subgroup_columns if roles.get(name) != "group"]
        if invalid_subgroups:
            raise ValueError(
                f"experiment subgroup columns do not match snapshot roles: {invalid_subgroups}"
            )
        if (
            spec.split.strategy == "temporal"
            and roles.get(spec.split.timestamp_column) != "timestamp"
        ):
            raise ValueError("temporal split column must have timestamp role")
        if spec.split.strategy == "group" and roles.get(spec.split.group_column) != "group":
            raise ValueError("group split column must have group role")

    def _audit(
        self,
        *,
        event_type: str,
        task_id,
        actor_id: str,
        tenant_id: str,
        trace_id: str,
        payload: dict[str, object],
    ) -> None:
        if self.audit is not None:
            self.audit.emit(
                AuditEvent(
                    event_type=event_type,
                    task_id=task_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    payload=payload,
                )
            )
