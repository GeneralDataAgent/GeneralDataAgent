"""Bounded offline candidate capture with a complete, resumeless batch ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from generaldataagent.business.experience import ExperienceStore
from generaldataagent.contracts import ActorContext
from generaldataagent.experience.extraction import extract_rule_experience

MAX_ITEMS = 100
MAX_FILE_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 20 * MAX_FILE_BYTES


def _read(path: Path, root: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("input_unavailable")
    with resolved.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("input_too_large")
    return value


def capture_batch(
    manifest: Path, output: Path, *, actor: ActorContext, created_at: datetime, extractor_hash: str
) -> dict:
    """Capture a new batch; partial failures never remove items from its denominator.

    Inputs are relative to the manifest directory. Exact duplicate byte pairs
    reference the first successful processing result within this batch only.
    Completed capture is not review, answer validation, or source authentication.
    """
    if not isinstance(actor, ActorContext) or created_at.tzinfo is None:
        raise ValueError("invalid_batch_context")
    if not re.fullmatch(r"[0-9a-f]{64}", extractor_hash):
        raise ValueError("invalid_extractor_hash")
    root = manifest.resolve().parent
    manifest_bytes = _read(manifest, root)
    entries = json.loads(manifest_bytes)
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ITEMS:
        raise ValueError("invalid_batch_manifest")
    identifiers = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "trace", "documents"}
            or not isinstance(entry["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry["id"])
            or entry["id"] in identifiers
            or any(
                not isinstance(entry[key], str) or not entry[key] for key in ("trace", "documents")
            )
            or any(Path(entry[key]).is_absolute() for key in ("trace", "documents"))
        ):
            raise ValueError("invalid_batch_manifest")
        identifiers.add(entry["id"])
    # Exclusive creation prevents accidental overwrite and reuse of prior candidates.
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": "experience-batch.v1",
        "run_status": "running",
        "manifest_hash": hashlib.sha256(manifest_bytes).hexdigest(),
        "extractor_hash": extractor_hash,
        "scope_hash": ExperienceStore._scope(actor),
        "planned_items": len(entries),
        "items": [{"id": item["id"], "status": "pending"} for item in entries],
        "independent_review": "not_run",
        "answer_correctness": "not_evaluated",
        "source_authenticated": False,
        "production_accepted": False,
        "model_calls": 0,
    }

    def save():
        report["counts"] = dict(Counter(item["status"] for item in report["items"]))
        report["candidate_count"] = sum(
            len(item.get("candidate_ids", [])) for item in report["items"]
        )
        temporary = output / "report.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "report.json")

    save()
    store = ExperienceStore(output / "candidates.sqlite", clock=lambda: created_at)
    seen: dict[tuple[str, str], str] = {}
    consumed = 0

    def read_input(path):
        nonlocal consumed
        remaining = MAX_BATCH_BYTES - consumed
        if remaining <= 0:
            raise ValueError("batch_byte_budget")
        limit = min(MAX_FILE_BYTES, remaining)
        try:
            value = _read(path, root, limit)
        except ValueError as exc:
            if str(exc) == "input_too_large":
                consumed += limit + 1  # One bounded byte probes the size limit.
                if limit < MAX_FILE_BYTES:
                    raise ValueError("batch_byte_budget") from exc
            raise
        consumed += len(value)
        return value

    for entry, result in zip(entries, report["items"], strict=True):
        try:
            trace_bytes = read_input(root / entry["trace"])
            document_bytes = read_input(root / entry["documents"])
            pair = tuple(
                hashlib.sha256(value).hexdigest() for value in (trace_bytes, document_bytes)
            )
            result.update(trace_hash=pair[0], documents_hash=pair[1])
            if pair in seen:
                result.update(status="duplicate", duplicate_of=seen[pair])
            else:
                trace = json.loads(trace_bytes)
                if not isinstance(trace, list) or not all(isinstance(item, dict) for item in trace):
                    raise ValueError("invalid_trace")
                candidates = extract_rule_experience(
                    trace,
                    documents=document_bytes.decode("utf-8"),
                    owner=actor.user_id,
                    created_at=created_at,
                )
                ids = []
                for candidate in candidates:
                    ids.append(
                        store.capture_from_extraction(
                            candidate,
                            actor,
                            trace_bytes=trace_bytes,
                            document_bytes=document_bytes,
                            extractor_hash=extractor_hash,
                        )
                    )
                result.update(status="captured" if ids else "no_candidate", candidate_ids=ids)
                seen[pair] = entry["id"]
        except (ValueError, OSError, sqlite3.Error) as exc:
            # Never copy document text, trace values or exception messages into the ledger.
            allowed = {"input_unavailable", "input_too_large", "batch_byte_budget", "invalid_trace"}
            code = (
                str(exc)
                if type(exc) is ValueError and str(exc) in allowed
                else "item_processing_failed"
            )
            result.update(status="rejected", code=code)
        report["input_bytes_read"] = consumed
        save()
    report["run_status"] = (
        "completed_with_errors" if report["counts"].get("rejected") else "completed"
    )
    save()
    return report
