"""Pin the optional runner treatment before any provider construction."""

import importlib.util
import json
from pathlib import Path

import pytest
from test_rule_document_extension import extension

from generaldataagent.business.rules.document_context import MarkdownDocument

RUNNER = Path(__file__).resolve().parents[1] / "evals/run_stage10_rule_supervisor.py"


def runner():
    spec = importlib.util.spec_from_file_location("rule_document_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configuration(tmp_path):
    module = runner()
    # The fixture root is inside the workspace, as are the production runner's inputs.
    module.ROOT = tmp_path
    document = MarkdownDocument("manual.md", "# Scope\r\nThe scope is local.\r\n", "v1")
    admitted = extension(tmp_path, [document], actor=module.ACTOR)
    (tmp_path / "manual.md").write_bytes(document.text.encode())
    (tmp_path / "skill.json").write_text(admitted.skill.model_dump_json())
    config = {
        "registry_path": "admission.sqlite",
        "skill_path": "skill.json",
        "skill_hash": admitted.admission.skill_hash(admitted.skill),
        "documents": [
            {
                "path": "manual.md",
                "source": "manual.md",
                "version": "v1",
                "content_hash": document.content_hash,
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return module, path, config, admitted


def test_runner_loads_explicit_reviewed_pinned_snapshot(tmp_path):
    module, path, _, admitted = configuration(tmp_path)
    loaded = module.load_document_extension(path)
    assert loaded.summary() == admitted.summary()
    assert loaded.context.search("scope")["matches"][0]["start_line"] == 2


@pytest.mark.parametrize("changed", ["manual.md", "skill.json"])
def test_replaced_bytes_require_updated_pins(tmp_path, changed):
    module, path, _, _ = configuration(tmp_path)
    target = tmp_path / changed
    if changed == "manual.md":
        target.write_text("The scope changed.")
    else:
        skill = json.loads(target.read_text())
        skill["prompt_template"] += " changed"
        target.write_text(json.dumps(skill))
    with pytest.raises(ValueError, match="pinned hash"):
        module.load_document_extension(path)


def test_revoked_skill_cannot_start_a_treatment(tmp_path):
    module, path, _, admitted = configuration(tmp_path)
    admitted.admission.registry.revoke(admitted.skill.name, admitted.skill.version)
    with pytest.raises(PermissionError):
        module.load_document_extension(path)


@pytest.mark.parametrize("change", ["outside", "unknown", "unpinned", "missing_registry"])
def test_runner_refuses_unbounded_or_unpinned_configuration(tmp_path, change):
    module, path, config, _ = configuration(tmp_path)
    if change == "outside":
        config["documents"][0]["path"] = "../outside.md"
    elif change == "unknown":
        config["extra"] = True
    elif change == "unpinned":
        del config["documents"][0]["content_hash"]
    else:
        config["registry_path"] = "absent.sqlite"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        module.load_document_extension(path)
    assert not (tmp_path / "absent.sqlite").exists()
