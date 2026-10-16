"""Policy-gated orchestration for reproducible fixed-template experiments."""

from dataclasses import dataclass

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import (
    AgentRole,
    AuditEvent,
    DatasetSnapshot,
    ExperimentReport,
    ExperimentSpec,
    MetricValue,
    ModelCard,
    RiskLevel,
    ScienceCheck,
    ToolRequest,
)
from generaldataagent.data_science.checks import split_indices, validate_experiment_data
from generaldataagent.data_science.sandbox import FixedTemplateSandbox, OCIContainerSandbox
from generaldataagent.data_science.snapshot import load_snapshot_rows
from generaldataagent.data_science.tracking import SQLiteExperimentTracker
from generaldataagent.observability import JsonlAuditSink
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
        audit: JsonlAuditSink | None = None,
        tracker: SQLiteExperimentTracker | None = None,
    ) -> None:
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
        if spec.task_id != snapshot.task_id or spec.snapshot_id != snapshot.snapshot_id:
            raise ValueError("experiment must bind the exact task and snapshot")
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
                "template_version": "1.0",
            },
            data_domains=(data_domain,),
            requested_risk=RiskLevel.R1,
        )
        decision = self.policy.evaluate(request)
        active_trace_id = trace_id or f"experiment-{spec.experiment_id}"
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
        rows = load_snapshot_rows(snapshot, self.artifact_store)
        train, test = split_indices(rows, spec)
        checks = validate_experiment_data(rows, spec, train, test)
        if any(not check.passed and check.severity == "error" for check in checks):
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
        result = self.sandbox.run(
            rows=rows,
            spec=spec,
            train_indices=train,
            test_indices=test,
        )
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
