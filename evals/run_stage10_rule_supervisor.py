"""Real-model run of the rule Supervisor over the public development questions.

This measures the **model** path: the Supervisor receives one parsed request and
must drive discovery → data-analyst → verifier → reporter through the tools it
was given. The deterministic workflow still computes and verifies every number;
the model only decides whether and when to call each role.

It is not a benchmark score. The parse step happens before the model and is
recorded as its own outcome, so a question the deterministic parser refuses
never reaches the model and is never counted as a model success.

The annex semantics reading is a required argument. Its fingerprint is written
into the report: two runs with different readings are not comparable.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from uuid import uuid4

from langchain_core.rate_limiters import InMemoryRateLimiter

from generaldataagent.agents.rule_supervisor import RuleDocumentExtension, RuleSupervisorRuntime
from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import CapabilityRegistry, FeedbackRegistry
from generaldataagent.business.rules.delivery import RuleDeliveryRuntime
from generaldataagent.business.rules.document_context import DocumentContext, MarkdownDocument
from generaldataagent.business.rules.governed import GovernedRuleRunner
from generaldataagent.business.rules.payments import SQLitePaymentsSource
from generaldataagent.business.rules.question import RuleQuestionError, parse_rule_question
from generaldataagent.business.rules.semantics import AnnexSemanticsDeclaration
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.rule_diagnostics import (
    RuleCallDiagnostics,
    stop_diagnostic,
    summarize_rule_run,
)
from generaldataagent.evaluation.stage_six import add_json_tables, build_csv_database, digest
from generaldataagent.evaluation.stage_six_control import ServiceCircuitBreaker
from generaldataagent.evaluation.stage_six_models import (
    build_evaluation_model,
    resolve_evaluation_model,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.checkpoints import open_protected_checkpointer
from generaldataagent.orchestration.rule_workflow import RuleWorkflowRuntime

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/stage6/dabstep"
ACTOR = ActorContext(
    user_id="stage10-evaluation",
    tenant_id="evaluation",
    project_id="dabstep-rules",
    data_domains=("payments",),
)


def _assert_no_filesystem_tools() -> None:
    """Refuse to run a model against a harness that can reach the filesystem.

    The run is a measurement only if the specialists cannot write or delete
    anything. This inspects the exclusion set itself; it never calls the
    registration path, because a patched or skipped registration would disable
    the very control it is checking.
    """
    from deepagents.middleware.filesystem import _ALL_FS_TOOL_NAMES

    from generaldataagent.agents import factory

    missing = set(_ALL_FS_TOOL_NAMES) - factory._GOVERNED_EXCLUDED_TOOLS
    if missing:
        raise RuntimeError(f"filesystem tools are reachable: {sorted(missing)}")


def _numeric_match(actual: str, expected: str, *, relative: float = 1e-6) -> bool:
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= relative * max(1.0, abs(b))


def _audit_lines(sink: JsonlAuditSink) -> int:
    if not sink.path.exists():
        return 0
    with sink.path.open("rb") as stream:
        return sum(1 for _ in stream)


def _artifacts_present(store: LocalArtifactStore, uris) -> bool:
    """Every advertised reference must resolve to stored, non-empty bytes."""
    prefix = "artifact://local/"
    for uri in uris:
        if not uri.startswith(prefix):
            return False
        path = (store.root / uri.removeprefix(prefix)).resolve()
        if store.root not in path.parents or not path.is_file() or path.stat().st_size == 0:
            return False
    return bool(uris)


def _matches(operation: str, delivery: dict, published: str) -> bool:
    expected = published.strip()
    if operation in {"select", "applicable"}:
        actual = sorted(str(item) for item in delivery.get("items") or [])
        want = sorted(item.strip() for item in expected.split(",") if item.strip())
        return actual == want
    key = "average_fee" if operation == "average_fee" else "delta"
    value = delivery.get(key)
    return value is not None and _numeric_match(str(value), expected)


@contextmanager
def _retained_runtime(output: Path):
    """Keep local development evidence after normal exit or an exception."""
    folder = output.with_name(output.stem + "-runtime")
    folder.mkdir(mode=0o700, exist_ok=False)
    key = token_bytes(32)
    descriptor = os.open(folder / "checkpoint.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
    yield folder, key


def _workspace_file(value: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("document configuration paths must be strings")
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("document configuration files must exist inside the repository")
    return path


def load_document_extension(path: Path) -> RuleDocumentExtension:
    """Load an explicit pinned treatment; never create or activate admission records."""
    path = _workspace_file(str(path))
    if path.stat().st_size > 128_000:
        raise ValueError("document configuration is too large")
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != {
        "registry_path",
        "skill_path",
        "skill_hash",
        "documents",
    }:
        raise ValueError("invalid document extension configuration")
    entries = config["documents"]
    if not isinstance(entries, list) or len(entries) > 32:
        raise ValueError("document extension requires at most 32 explicit documents")
    documents = []
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "source",
            "version",
            "content_hash",
        }:
            raise ValueError("invalid pinned document entry")
        source = _workspace_file(entry["path"])
        size = source.stat().st_size
        total_bytes += size
        if size > 512_000 or total_bytes > 1_024_000:
            raise ValueError("document input exceeds byte budget")
        document = MarkdownDocument(
            source=entry["source"],
            version=entry["version"],
            text=source.read_bytes().decode("utf-8"),
        )
        if document.content_hash != entry["content_hash"]:
            raise ValueError("document content differs from pinned hash")
        documents.append(document)
    skill_path = _workspace_file(config["skill_path"])
    if skill_path.stat().st_size > 128_000:
        raise ValueError("document skill input is too large")
    skill = SkillDefinition.model_validate_json(skill_path.read_bytes())
    if ExtensionAdmission.skill_hash(skill) != config["skill_hash"]:
        raise ValueError("document skill differs from pinned hash")
    admission = ExtensionAdmission(
        CapabilityRegistry(
            _workspace_file(config["registry_path"]), clock=lambda: datetime.now(UTC)
        )
    )
    extension = RuleDocumentExtension(DocumentContext(documents), admission, skill)
    # Check admission before resolving or invoking any provider. Request-specific
    # template validation is repeated by the runtime for every bound task.
    admission.require("skill", skill.name, skill.version, config["skill_hash"], ACTOR)
    return extension


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-type", choices=["claude", "codex", "magpie"], default="magpie")
    parser.add_argument("--provider-id", default="magpie")
    parser.add_argument("--model", required=True)
    parser.add_argument("--document-extension", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-runtime-seconds", type=float, default=120)
    parser.add_argument("--request-interval-seconds", type=float, default=3.5)
    parser.add_argument(
        "--empty-list-is-wildcard", action=argparse.BooleanOptionalAction, required=True
    )
    parser.add_argument(
        "--semantics-excerpt",
        default=(
            "If a field is set to null it means that it applies to all possible "
            "values of that field."
        ),
    )
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT):
        raise ValueError("evaluation output must stay inside the implementation repository")
    if args.output.exists() or args.output.with_name(args.output.stem + "-runtime").exists():
        raise FileExistsError("evaluation output or retained runtime already exists")
    if not 1 <= args.limit <= 10:
        raise ValueError("limit must be between one and ten")
    if not math.isfinite(args.max_runtime_seconds) or args.max_runtime_seconds <= 0:
        raise ValueError("runtime must be finite and positive")
    if not 0 <= args.request_interval_seconds <= 60:
        raise ValueError("request interval must be between zero and sixty seconds")

    documents = "\n".join(
        (DATASET / name).read_text() for name in ("manual.md", "payments-readme.md")
    )
    declaration = AnnexSemanticsDeclaration(
        empty_list_is_wildcard=args.empty_list_is_wildcard,
        source_excerpt=args.semantics_excerpt,
    ).validated_against(documents)
    merchants = tuple(
        entry["merchant"] for entry in json.loads((DATASET / "merchant_data.json").read_text())
    )
    mcc = {
        row["description"]: int(row["mcc"])
        for row in csv.DictReader((DATASET / "merchant_category_codes.csv").open())
    }
    questions = [json.loads(line) for line in (DATASET / "dev.jsonl").read_text().splitlines()][
        : args.limit
    ]
    annex = json.loads((DATASET / "fees.json").read_text())

    document_extension = (
        load_document_extension(args.document_extension) if args.document_extension else None
    )
    provider, route = resolve_evaluation_model(
        app_type=args.app_type,
        provider_id=args.provider_id,
        model_override=args.model,
        transport="openai-chat",
        thinking_mode="default",
        json_mode=False,
    )
    # The measurement is only meaningful if a specialist cannot touch the host
    # filesystem; fail the run rather than silently measure an unguarded agent.
    _assert_no_filesystem_tools()
    model = build_evaluation_model(provider, route)
    diagnostics = RuleCallDiagnostics()
    model.callbacks = [diagnostics]
    if args.request_interval_seconds:
        model.rate_limiter = InMemoryRateLimiter(
            requests_per_second=1 / args.request_interval_seconds, max_bucket_size=1
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "runner_hash": digest(Path(__file__)),
        "supervisor_hash": digest(ROOT / "src/generaldataagent/agents/rule_supervisor.py"),
        "role_controller_hash": digest(ROOT / "src/generaldataagent/agents/supervision.py"),
        "completion_hash": digest(ROOT / "src/generaldataagent/agents/completion.py"),
        "diagnostics_hash": digest(ROOT / "src/generaldataagent/evaluation/rule_diagnostics.py"),
        "factory_hash": digest(ROOT / "src/generaldataagent/agents/factory.py"),
        "document_context_hash": digest(
            ROOT / "src/generaldataagent/business/rules/document_context.py"
        ),
        "governed_runner_hash": digest(ROOT / "src/generaldataagent/business/rules/governed.py"),
        "document_extension": document_extension.summary()
        if document_extension
        else {"enabled": False},
        "document_configuration_hash": digest(_workspace_file(str(args.document_extension)))
        if document_extension
        else None,
        "dataset_hashes": {
            path.name: digest(path) for path in sorted(DATASET.iterdir()) if path.is_file()
        },
        "planned_task_ids": [question["task_id"] for question in questions],
        "run_status": "running",
        "service_policy": {"max_consecutive_failures": 3, "terminal_http": [401, 402, 403, 404]},
        "annex_semantics": declaration.summary(),
        "model_route": route,
        "evaluation": "real-model-rule-supervisor",
        "public_benchmark_score": False,
        "production_accepted": False,
        "cost_usd": None,
        "started_at": datetime.now(UTC).isoformat(),
        "results": [],
        "retained_runtime": {
            "directory": args.output.stem + "-runtime",
            "checkpoint": "state",
            "payments": "payments.sqlite",
            "key_management": "local_file_owner_only_development_not_production",
            "supervisor_checkpoints": "in_memory_not_retained",
        },
    }

    circuit = ServiceCircuitBreaker()

    def persist() -> None:
        report["service_circuit_reason"] = circuit.reason
        report["summary"] = summarize_rule_run(report["results"], planned_n=len(questions))
        temporary = args.output.with_suffix(".partial")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(args.output)

    def record(row: dict) -> None:
        if row["outcome"] in {"model_run", "supervisor_error"}:
            circuit.observe(
                {
                    "failure": "model_error" if row.get("model_errors") else None,
                    "trace": row.get("model_errors", []),
                }
            )
        if row["outcome"] in {"model_run", "supervisor_error"}:
            row["diagnostic_events"] = list(diagnostics.events)
        row["stop_diagnostic"] = stop_diagnostic(row)
        report["results"].append(row)
        persist()

    persist()
    with _retained_runtime(args.output) as (folder, checkpoint_key):
        database = Path(folder) / "payments.sqlite"
        build_csv_database(
            {
                name: DATASET / (name + ".csv")
                for name in ("payments", "acquirer_countries", "merchant_category_codes")
            },
            database,
        )
        add_json_tables(
            {name: DATASET / (name + ".json") for name in ("fees", "merchant_data")},
            database,
        )
        services = AnnexWorkflowServices(
            annex,
            documents=documents,
            payments=SQLitePaymentsSource(
                database, allowed_relations={"merchant_data", "payments"}
            ),
        )
        artifacts = LocalArtifactStore(args.output.parent / "artifacts")
        audit = JsonlAuditSink(args.output.parent / "audit.jsonl")
        with open_protected_checkpointer(folder / "state", encryption_key=checkpoint_key) as saver:
            flow = RuleWorkflowRuntime(
                services=services.services(),
                checkpointer=saver,
                source_hashes=services.source_hashes,
                workflow_version="stage10-rule-supervisor",
            )
            for question in questions:
                row = {"task_id": question["task_id"], "level": question["level"]}
                try:
                    request = parse_rule_question(
                        question["question"],
                        known_merchants=merchants,
                        semantics_excerpt=args.semantics_excerpt,
                        mcc_descriptions=mcc,
                        empty_list_is_wildcard=args.empty_list_is_wildcard,
                    )
                except RuleQuestionError as exc:
                    row.update(
                        outcome="refused_by_parser",
                        refusal_code=exc.code,
                        model_calls=0,
                        passed=False,
                    )
                    record(row)
                    print(
                        json.dumps({"task_id": row["task_id"], "outcome": row["outcome"]}),
                        flush=True,
                    )
                    continue
                declaration.apply_to(request)
                row["operation"] = request.operation
                if circuit.reason:
                    row.update(
                        outcome="service_circuit_open",
                        attempted=False,
                        service_blocker=circuit.reason,
                        model_calls=0,
                        model_attempts=0,
                        elapsed_seconds=0.0,
                        passed=False,
                    )
                    record(row)
                    continue
                row["attempted"] = True
                task_id = uuid4()
                row["rule_thread_id"] = str(task_id)
                row["supervisor_thread_id"] = f"stage10-{task_id}"
                row["request"] = request.model_dump(mode="json")
                delivery = RuleDeliveryRuntime(
                    workflow=flow,
                    artifacts=artifacts,
                    feedback=FeedbackRegistry(args.output.parent / "feedback.sqlite"),
                    audit=audit,
                )
                supervisor = RuleSupervisorRuntime(
                    model=model,
                    request=request,
                    workflow=flow,
                    delivery=delivery,
                    task_id=task_id,
                    semantics=declaration,
                    document_extension=document_extension,
                    # Execution goes through ``rules.execute`` policy and audit;
                    # the model never calls the workflow directly.
                    runner=GovernedRuleRunner(
                        runtime=flow, actor=ACTOR, artifacts=artifacts, audit=audit
                    ),
                )
                diagnostics.events.clear()
                audit_before = _audit_lines(audit)
                started = time.monotonic()
                try:
                    result = await supervisor.arun(
                        ACTOR,
                        thread_id=f"stage10-{task_id}",
                        max_runtime_seconds=args.max_runtime_seconds,
                    )
                except Exception as exc:
                    row.update(
                        outcome="supervisor_error",
                        error=type(exc).__name__,
                        **diagnostics.snapshot(),
                        audit_events=_audit_lines(audit) - audit_before,
                        elapsed_seconds=time.monotonic() - started,
                        passed=False,
                    )
                    record(row)
                    print(
                        json.dumps({"task_id": row["task_id"], "outcome": row["outcome"]}),
                        flush=True,
                    )
                    continue
                delivery_package = None
                if result.status == "completed" and result.artifact_uris:
                    try:
                        # The rule workflow's thread id is the task id; the
                        # supervisor's own checkpoint thread is unrelated.
                        state = flow.state_of(str(task_id), request, ACTOR)
                        delivery_package = state.get("delivery")
                    except Exception:
                        delivery_package = None
                matched = (
                    _matches(request.operation, delivery_package, str(question["answer"]))
                    if delivery_package
                    else False
                )
                roles_complete = tuple(result.delegated_roles) == (
                    "discovery",
                    "data-analyst",
                    "verifier",
                    "reporter",
                )
                evidence_present = _artifacts_present(artifacts, result.artifact_uris)
                row.update(
                    outcome="model_run",
                    status=result.status,
                    summary=result.summary[:400],
                    limitations=list(result.limitations),
                    delegated_roles=list(result.delegated_roles),
                    roles_complete=roles_complete,
                    artifact_uris=list(result.artifact_uris),
                    evidence_present=evidence_present,
                    audit_events=_audit_lines(audit) - audit_before,
                    answer_matches_published=matched,
                    elapsed_seconds=time.monotonic() - started,
                    **diagnostics.snapshot(),
                    passed=bool(
                        result.status == "completed"
                        and roles_complete
                        and matched
                        and evidence_present
                    ),
                )
                record(row)
                print(
                    json.dumps(
                        {
                            "task_id": row["task_id"],
                            "outcome": row["outcome"],
                            "status": row["status"],
                            "roles": row["delegated_roles"],
                            "passed": row["passed"],
                            "model_calls": row["model_calls"],
                            "elapsed": round(row["elapsed_seconds"], 1),
                        }
                    ),
                    flush=True,
                )

    report["run_status"] = "blocked_by_service" if circuit.reason else "completed"
    report["completed_at"] = datetime.now(UTC).isoformat()
    persist()
    print(json.dumps(report["summary"], indent=2), flush=True)
    if circuit.reason:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
