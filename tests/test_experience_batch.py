import json
from pathlib import Path

import pytest
from test_stage10_experience_extraction import ACTOR, DOCUMENTS, NOW
from test_stage10_experience_extraction import recovered as recovered_trace

from generaldataagent.business.experience import ExperienceStore
from generaldataagent.business.experience_batch import capture_batch


@pytest.fixture
def inputs(tmp_path):
    trace = recovered_trace.__wrapped__(tmp_path)
    (tmp_path / "trace.json").write_text(json.dumps(trace))
    (tmp_path / "documents.txt").write_text(DOCUMENTS)
    return tmp_path


def run(root, entries, output="out"):
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(entries))
    return capture_batch(
        manifest, root / output, actor=ACTOR, created_at=NOW, extractor_hash="a" * 64
    )


def item(name, trace="trace.json", documents="documents.txt"):
    return dict(id=name, trace=trace, documents=documents)


def test_mixed_batch_keeps_denominator_and_only_captures_reviewable_candidates(inputs):
    (inputs / "empty.json").write_text("[]")
    (inputs / "bad.json").write_text('{"secret": "private answer sentinel"}')
    report = run(
        inputs,
        [
            item("ok"),
            item("duplicate"),
            item("empty", "empty.json"),
            item("invalid", "bad.json"),
            item("missing", "missing.json"),
        ],
    )
    assert report["planned_items"] == 5
    assert report["counts"] == {"captured": 1, "duplicate": 1, "no_candidate": 1, "rejected": 2}
    assert report["run_status"] == "completed_with_errors"
    assert report["candidate_count"] == 1
    assert report["items"][1]["duplicate_of"] == "ok"
    assert "sentinel" not in json.dumps(report)
    assert report["production_accepted"] is False
    store = ExperienceStore(inputs / "out/candidates.sqlite", clock=lambda: NOW, read_only=True)
    assert store.audit()["by_state"] == {"candidate": 1}
    assert (
        store.retrieve(failure_class="rule_semantics_excerpt_missing", tool="rules", actor=ACTOR)
        == ()
    )
    from uuid import UUID

    receipt = store.extraction_source(UUID(report["items"][0]["candidate_ids"][0]), ACTOR)
    assert receipt["trace_hash"] == report["items"][0]["trace_hash"]
    assert receipt["documents_hash"] == report["items"][0]["documents_hash"]


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [item("same"), item("same")],
        [dict(id="x", trace="a")],
        [item("x", "/absolute")],
        [item("x")] * 101,
    ],
)
def test_invalid_manifest_creates_no_output(inputs, entries):
    with pytest.raises(ValueError, match="invalid_batch_manifest"):
        run(inputs, entries)
    assert not (inputs / "out").exists()


def test_existing_output_preserved(inputs):
    run(inputs, [item("first")])
    before = (inputs / "out/report.json").read_bytes()
    with pytest.raises(FileExistsError):
        run(inputs, [item("second")])
    assert (inputs / "out/report.json").read_bytes() == before


@pytest.mark.parametrize("symlink", [False, True])
def test_input_cannot_escape_manifest_directory(inputs, symlink):
    outside = inputs.parent / "outside.json"
    outside.write_text("[]")
    name = "../outside.json"
    if symlink:
        (inputs / "link.json").symlink_to(outside)
        name = "link.json"
    report = run(inputs, [item("escape", name), item("valid")])
    assert report["items"][0]["code"] == "input_unavailable"
    assert report["candidate_count"] == 1


def test_budget_exhaustion_preserves_remaining_denominator(inputs, monkeypatch):
    import generaldataagent.business.experience_batch as module

    monkeypatch.setattr(module, "MAX_BATCH_BYTES", 10)
    report = run(inputs, [item("a"), item("b"), item("c")])
    assert report["counts"] == {"rejected": 3}
    assert report["input_bytes_read"] == 11
    assert all(row["code"] == "batch_byte_budget" for row in report["items"])


def test_unexpected_interruption_leaves_running_ledger(inputs, monkeypatch):
    import generaldataagent.business.experience_batch as module

    def stop(*args, **kwargs):
        raise RuntimeError("stop")

    monkeypatch.setattr(module, "extract_experiences", stop)
    with pytest.raises(RuntimeError):
        run(inputs, [item("a"), item("b")])
    report = json.loads((inputs / "out/report.json").read_text())
    assert report["run_status"] == "running"
    assert report["counts"] == {"pending": 2}


def test_cli_exit_code_signals_partial_failure(inputs, monkeypatch, capsys):
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "evals/extract_stage10_experience_batch.py"
    spec = importlib.util.spec_from_file_location("batch_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = inputs
    monkeypatch.setattr(module, "extraction_source_hash", lambda root: "a" * 64)
    manifest = inputs / "manifest.json"
    manifest.write_text(json.dumps([item("ok"), item("missing", "absent.json")]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(path),
            "--manifest",
            str(manifest),
            "--output",
            str(inputs / "out"),
            "--actor",
            ACTOR.model_dump_json(),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["planned_items"] == 2
