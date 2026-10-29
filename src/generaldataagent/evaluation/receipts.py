"""Opt-in receipts for fresh evaluation steps; legacy reports are never upgraded."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import (
    ActorContext,
    ArtifactRef,
    ExecutionMethod,
    ExecutionReceipt,
    ObservedUsage,
    ReceiptVerification,
    SourceBinding,
    load_receipt,
)

ADAPTER_VERSION = "evaluation-step-v1"

# One definition of the check this adapter performs, shared by the writer and the
# reader. A stored step whose verification differs from this in strength is a
# different claim, not an equivalent one, so the reader refuses it by name rather
# than reporting the same generic binding failure it uses for identity drift.
ADAPTER_VERIFICATION = ReceiptVerification(
    scope="producer_self_check",
    passed=True,
    check_codes=("result_artifact_readback", "step_result_hash"),
)


class EvaluationReceiptError(ValueError):
    """Evidence could not be recorded; never ask the model to repair storage."""


def result_bytes(value: dict) -> bytes:
    # Preserve the legacy step/binding hash exactly, including its spacing and
    # string conversion. Receipt adoption must not rewrite historical evidence.
    return json.dumps(value, sort_keys=True, default=str).encode()


def _hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class EvaluationReceiptRecorder:
    """Research-only artifact recorder with explicit operator-supplied identity."""

    def __init__(
        self,
        *,
        artifacts: LocalArtifactStore,
        actor: ActorContext,
        task_id: UUID,
        trace_id: str,
        task,
        database: Path,
        documents: str,
        profile: str,
    ):
        if not isinstance(artifacts, LocalArtifactStore):
            raise ValueError("evaluation_receipts_require_local_research_store")
        self.artifacts, self.actor, self.task_id = artifacts, actor, task_id
        self.database = database.resolve()
        self.task_payload = asdict(task)
        self.documents = documents
        # Hash file bytes without importing stage_six (the recorder is also used
        # by that module). This is a static development snapshot, not a DB lease.
        self.database_hash = self._database_hash()
        self.sources = (
            SourceBinding(source_id="database", content_hash=self.database_hash),
            SourceBinding(source_id="documents", content_hash=_hash(documents.encode())),
            SourceBinding(source_id="public_task", content_hash=_hash(result_bytes(asdict(task)))),
        )
        package = Path(__file__).resolve().parents[1]
        files = (
            "evaluation/receipts.py",
            "evaluation/stage_six.py",
            "evaluation/stage_six_analysis.py",
            "contracts/receipt.py",
            "tools/answer_fields.py",
        )
        self.runtime_hash = _hash(
            result_bytes({name: _hash((package / name).read_bytes()) for name in files})
        )
        self.identity = {
            "task_id": str(task_id),
            "actor_id": actor.user_id,
            "tenant_id": actor.tenant_id,
            "project_id": actor.project_id,
            "trace_id": trace_id,
        }
        # Validate identity before any model call or artifact creation.
        ExecutionReceipt(
            **self.identity,
            action="evaluation.step",
            method=ExecutionMethod(method_id="evaluation.step", method_version=ADAPTER_VERSION),
            verification=ReceiptVerification(scope="none", passed=False),
        )
        self.profile = profile
        self.steps: list[dict] = []

    def require_inputs(self, task, database: Path, documents: str, profile: str):
        if (
            asdict(task) != self.task_payload
            or database.resolve() != self.database
            or documents != self.documents
            or profile != self.profile
        ):
            raise EvaluationReceiptError("evaluation_receipt_inputs_mismatch")
        self.check_sources()

    def _database_hash(self):
        # Queries read committed WAL pages as well as the main file. This
        # research adapter accepts only static, checkpointed database files.
        wal = self.database.with_name(self.database.name + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise EvaluationReceiptError("evaluation_receipt_active_wal")
        with self.database.open("rb") as stream:
            return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()

    def check_sources(self):
        if self._database_hash() != self.database_hash:
            raise EvaluationReceiptError("evaluation_receipt_database_changed")

    def record(self, event: dict, result: dict) -> None:
        try:
            self.check_sources()
            content = result_bytes(result)
            digest = hashlib.sha256(content).hexdigest()
            if event["result_hash"] != digest or any(
                entry["step"] == event["step"] for entry in self.steps
            ):
                raise ValueError("evaluation_receipt_step_binding_invalid")
            ref = self.artifacts.put_bytes(
                task_id=self.task_id,
                artifact_type="evaluation.step.result",
                content=content,
                created_by=self.actor.user_id,
                extension="json",
            )
            if ref.task_id != self.task_id or self.artifacts.read_bytes(ref) != content:
                raise ValueError("evaluation_receipt_artifact_mismatch")
            receipt = ExecutionReceipt(
                **self.identity,
                action="evaluation.step",
                sources=self.sources,
                method=ExecutionMethod(
                    method_id="evaluation." + event["tool"],
                    method_version=ADAPTER_VERSION,
                    runtime_fingerprint=self.runtime_hash,
                ),
                result_refs=(ref,),
                completeness="partial" if result.get("complete") is False else "complete",
                usage=ObservedUsage(tool_calls=1),
                verification=ADAPTER_VERIFICATION,
            )
            self.check_sources()
            self.steps.append(
                {
                    "step": event["step"],
                    "tool": event["tool"],
                    "method_id": result.get("method_id") or result.get("operation"),
                    "result_hash": digest,
                    "receipt": receipt.model_dump(mode="json"),
                }
            )
        except EvaluationReceiptError:
            raise
        except Exception as exc:
            raise EvaluationReceiptError("evaluation_receipt_recording_failed") from exc

    def export(self) -> dict:
        self.check_sources()
        output = {
            "receipt_context": {
                **self.identity,
                "sources": [source.model_dump(mode="json") for source in self.sources],
                "profile": self.profile,
                "adapter_version": ADAPTER_VERSION,
                "runtime_fingerprint": self.runtime_hash,
            },
            "execution_receipts": list(self.steps),
        }
        validate_evaluation_receipts(output, read_bytes=self.artifacts.read_bytes)
        return output


def validate_evaluation_receipts(
    row: dict, *, read_bytes: Callable[[ArtifactRef], bytes] | None = None
) -> tuple[ExecutionReceipt, ...]:
    """Read legacy rows unchanged; validate fresh receipt consistency and optional bytes.

    Embedded identity is an explicit research declaration, not authenticated
    provenance. This checker does not certify answer semantics or benchmark scores.
    """
    if "execution_receipts" not in row and "receipt_context" not in row:
        return ()
    context, entries = row.get("receipt_context"), row.get("execution_receipts")
    if not isinstance(context, dict) or not isinstance(entries, list):
        raise ValueError("evaluation_receipt_envelope_invalid")
    identity = ("task_id", "actor_id", "tenant_id", "project_id", "trace_id")
    sources = tuple(SourceBinding.model_validate(value) for value in context["sources"])
    if (
        context["adapter_version"] != ADAPTER_VERSION
        or len(sources) != 3
        or {source.source_id for source in sources} != {"database", "documents", "public_task"}
    ):
        raise ValueError("evaluation_receipt_context_invalid")
    ExecutionReceipt(
        **{key: context[key] for key in identity},
        action="evaluation.step",
        sources=sources,
        method=ExecutionMethod(
            method_id="evaluation.step",
            method_version=ADAPTER_VERSION,
            runtime_fingerprint=context["runtime_fingerprint"],
        ),
        verification=ReceiptVerification(scope="none", passed=False),
    )
    seen, receipts = set(), []
    for entry in entries:
        step = entry["step"]
        if type(step) is not int or step < 1 or step in seen:
            raise ValueError("evaluation_receipt_step_invalid")
        seen.add(step)
        # Stored steps are read through the versioned entry point, so a step
        # written by another receipt version is refused by name instead of
        # failing as generic validation.
        receipt = load_receipt(entry["receipt"])
        # Strength is checked before identity: a step that claims an independent
        # or human verification is not this adapter's evidence, and reporting it
        # as a generic binding mismatch would hide that the claim itself differs.
        if receipt.verification != ADAPTER_VERIFICATION:
            raise ValueError("evaluation_receipt_verification_mismatch")
        if (
            any(getattr(receipt, key) != context[key] for key in identity)
            or receipt.sources != sources
            or receipt.action != "evaluation.step"
            or receipt.method.method_id != "evaluation." + entry["tool"]
            or receipt.method.method_version != context["adapter_version"]
            or receipt.method.runtime_fingerprint != context["runtime_fingerprint"]
            or len(receipt.result_refs) != 1
            or receipt.result_refs[0].content_hash != "sha256:" + entry["result_hash"]
        ):
            raise ValueError("evaluation_receipt_binding_mismatch")
        if "database_hash" in row and not any(
            source.source_id == "database"
            and source.content_hash == "sha256:" + row["database_hash"]
            for source in sources
        ):
            raise ValueError("evaluation_receipt_database_mismatch")
        if "trace" in row:
            events = [event for event in row["trace"] if event.get("step") == step]
            if (
                len(events) != 1
                or any(events[0].get(key) != entry[key] for key in ("tool", "result_hash"))
                or events[0].get("status") != "ok"
            ):
                raise ValueError("evaluation_receipt_trace_mismatch")
        if read_bytes is not None:
            content = read_bytes(receipt.result_refs[0])
            if _hash(content) != receipt.result_refs[0].content_hash:
                raise ValueError("evaluation_receipt_content_mismatch")
            result = json.loads(content)
            if result_bytes(result) != content:
                raise ValueError("evaluation_receipt_serialization_mismatch")
            method = result.get("method_id") or result.get("operation")
            if (
                method != entry["method_id"]
                or (entry["tool"] in {"sql", "sql_crosscheck"} and method not in {None, "sql"})
                or (entry["tool"] == "fixed_analysis" and result.get("operation") != method)
            ):
                raise ValueError("evaluation_receipt_method_mismatch")
        receipts.append(receipt)
    if "trace" in row and seen != {
        event["step"]
        for event in row["trace"]
        if event.get("status") == "ok" and "result_hash" in event
    }:
        raise ValueError("evaluation_receipt_missing_step")
    by_step = {entry["step"]: entry for entry in entries}
    # Answer bindings resolve through the receipts the versioned reader has
    # already accepted, never through a second parse of the stored dict.
    receipts_by_step = {
        entry["step"]: receipt for entry, receipt in zip(entries, receipts, strict=True)
    }
    for binding in row.get("answer_evidence", {}).values():
        entry = by_step.get(binding.get("step"))
        if entry is None or any(
            entry[key] != binding.get(key) for key in ("result_hash", "method_id")
        ):
            raise ValueError("evaluation_receipt_answer_binding_mismatch")
        if read_bytes is not None:
            value = json.loads(read_bytes(receipts_by_step[binding["step"]].result_refs[0]))
            for part in binding["path"]:
                value = value[part]
            if result_bytes({"value": value}) != result_bytes({"value": binding["raw_value"]}):
                raise ValueError("evaluation_receipt_answer_value_mismatch")
    return tuple(receipts)
