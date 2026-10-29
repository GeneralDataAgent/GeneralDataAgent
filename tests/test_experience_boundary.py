"""Product capture and pure derivation must work without benchmark modules."""

import builtins
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from test_stage10_experience_extraction import ACTOR, DOCUMENTS, NOW
from test_stage10_experience_extraction import recovered as recovered_trace

from generaldataagent.business.actions import content_hash
from generaldataagent.business.experience import ExperienceRecord as LegacyRecord
from generaldataagent.business.experience import ExperienceStore
from generaldataagent.contracts.experience import ExperienceRecord, ExtractedExperience
from generaldataagent.evaluation.experience_extraction import (
    ExtractedExperience as LegacyCandidate,
)
from generaldataagent.evaluation.experience_extraction import (
    extract_rule_experience as legacy_extract,
)
from generaldataagent.experience.extraction import (
    extract_rule_experience,
    extraction_source_hash,
    rule_repair_context_hash,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recovered(tmp_path):
    return recovered_trace.__wrapped__(tmp_path)


def test_legacy_imports_keep_identical_contracts_and_derivation(recovered):
    assert LegacyRecord is ExperienceRecord
    assert LegacyCandidate is ExtractedExperience
    assert legacy_extract is extract_rule_experience
    record = ExperienceRecord(
        experience_id=UUID(int=1),
        failure_class="rule_semantics_excerpt_missing",
        applies_when=("condition",),
        does_not_apply_when=("exception",),
        repair="repair",
        counterexample="counterexample",
        tool_scope=("rules",),
        owner="u",
        created_at=NOW,
    )
    assert content_hash(record.model_dump(mode="json")) == (
        "sha256:623b12aa66196d5cdf180efbac5916f6aebd645d4a0ba3c3dcdcba85fb8a7912"
    )
    body = record.model_dump(mode="json")
    assert ExperienceRecord.model_validate_json(record.model_dump_json()) == record
    assert content_hash(body) == content_hash(
        LegacyRecord.model_validate(body).model_dump(mode="json")
    )
    arguments = {"operation": "select", "semantics_excerpt": "ignored", "label": "中文"}
    assert rule_repair_context_hash(arguments) == content_hash(
        {"operation": "select", "label": "中文"}
    )


def test_product_capture_does_not_import_evaluation(recovered, tmp_path, monkeypatch):
    (candidate,) = extract_rule_experience(
        recovered, documents=DOCUMENTS, owner="u", created_at=NOW
    )
    store = ExperienceStore(tmp_path / "experience.sqlite", clock=lambda: NOW)
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("generaldataagent.evaluation"):
            raise AssertionError("product capture imported benchmark code")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    store.capture_from_extraction(
        candidate,
        ACTOR,
        trace_bytes=json.dumps(recovered).encode(),
        document_bytes=DOCUMENTS.encode(),
        extractor_hash=extraction_source_hash(ROOT),
    )
    assert store.state(candidate.record.experience_id, ACTOR) == "candidate"
    assert (
        store.retrieve(failure_class=candidate.record.failure_class, tool="rules", actor=ACTOR)
        == ()
    )


def test_fresh_product_and_neutral_imports_do_not_load_evaluation():
    script = """
import importlib.abc
import sys
class BlockEvaluation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('generaldataagent.evaluation'):
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockEvaluation())
from generaldataagent.experience.extraction import extract_rule_experience
from generaldataagent.business.experience import ExperienceStore
assert not any(name.startswith('generaldataagent.evaluation') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(
    "changed",
    [
        "contracts/experience.py",
        "experience/extraction.py",
        "experience/sql_extraction.py",
        "tools/sql_guard.py",
    ],
)
def test_source_fingerprint_covers_contract_and_implementation(tmp_path, changed):
    for relative in (
        "contracts/experience.py",
        "experience/extraction.py",
        "experience/sql_extraction.py",
        "tools/sql_guard.py",
    ):
        target = tmp_path / "src/generaldataagent" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / "src/generaldataagent" / relative).read_bytes())
    before = extraction_source_hash(tmp_path)
    target = tmp_path / "src/generaldataagent" / changed
    target.write_bytes(target.read_bytes() + b"\n# changed\n")
    assert extraction_source_hash(tmp_path) != before
