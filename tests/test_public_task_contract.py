"""A non-benchmark caller accepts sourced public requirements without loading an evaluator."""

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from generaldataagent.evaluation import action_protocol
from generaldataagent.evaluation.stage_seven import stage7_fingerprint
from generaldataagent.orchestration import analysis_plan, task_contract_cli, task_semantics

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/public-task-contract.json"


def test_public_contract_cli_in_clean_process_never_imports_evaluation_or_model_frameworks(
    tmp_path,
):
    output = tmp_path / "accepted.json"
    bootstrap = """
import importlib.abc
import runpy
import sys

blocked = ('generaldataagent.evaluation', 'langchain', 'langgraph', 'deepagents')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('unexpected runtime dependency: ' + fullname)
sys.meta_path.insert(0, Guard())
from generaldataagent.orchestration.analysis_plan import accept_contract
sys.argv = ['task-contract', '--request', sys.argv[1], '--output', sys.argv[2]]
runpy.run_module('generaldataagent.orchestration.task_contract_cli', run_name='__main__')
assert not any(name.startswith(blocked) for name in sys.modules)
"""
    before = EXAMPLE.read_bytes()
    completed = subprocess.run(
        [sys.executable, "-c", bootstrap, str(EXAMPLE), str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout == completed.stderr == ""
    accepted = json.loads(output.read_text())
    request = json.loads(before)
    assert accepted["question"] == request["question"]
    assert accepted["public_requirements"] == request["instructions"]
    assert accepted["answer"]["decimals"] == 2
    assert accepted["answer"]["columns"] == ["mean_duration"]
    assert accepted["answer"]["shape"] == "scalar"
    assert EXAMPLE.read_bytes() == before


def test_existing_imports_are_compatible_aliases():
    assert analysis_plan.accept_contract is task_semantics.accept_contract
    for name in (
        "ActionSchemaError",
        "PROTOCOL_ACTION_KEYS",
        "normalize_protocol_action",
        "validate_action",
    ):
        assert getattr(analysis_plan, name) is getattr(action_protocol, name)
    with pytest.raises(AttributeError):
        _ = analysis_plan.unknown_action


@pytest.mark.parametrize(
    "changed", ["orchestration/task_semantics.py", "evaluation/action_protocol.py"]
)
def test_moved_behavior_is_included_in_new_run_fingerprints(tmp_path, changed):
    original = stage7_fingerprint(ROOT)
    assert changed in original
    for name in original:
        target = tmp_path / "src/generaldataagent" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src/generaldataagent" / name, target)
    before = stage7_fingerprint(tmp_path)
    assert before == original
    target = tmp_path / "src/generaldataagent" / changed
    target.write_text(target.read_text() + "\n# changed implementation\n")
    after = stage7_fingerprint(tmp_path)
    assert {name for name in before if before[name] != after[name]} == {changed}


@pytest.mark.parametrize(
    "change",
    ["public_question", "public_instructions", "precision", "conflict", "unknown_field"],
)
def test_cli_rejects_unsourced_or_conflicting_proposals_without_an_output(tmp_path, capsys, change):
    request = json.loads(EXAMPLE.read_text())
    proposal = request["proposal"]
    if change == "public_question":
        proposal["question"] = "A different question"
    elif change == "public_instructions":
        proposal["public_requirements"] = ""
    elif change == "precision":
        proposal["answer"]["decimals"] = 3
    elif change == "conflict":
        proposal["conflicts"] = ["unresolved"]
    else:
        proposal["credentials"] = "must-not-appear-in-error-output"
    path, output = tmp_path / "request.json", tmp_path / "accepted.json"
    path.write_text(json.dumps(request))
    with pytest.raises(SystemExit) as caught:
        task_contract_cli.main(["--request", str(path), "--output", str(output)])
    assert caught.value.code == 2
    assert not output.exists()
    assert "must-not-appear-in-error-output" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "data",
    [
        b"{",
        b"[]",
        b'{"question": "x"}',
        b'{"question": 1, "instructions": "", "proposal": {}}',
        b'{"question": "x", "instructions": "", "proposal": {}, "extra": true}',
        b'{"question": "x", "instructions": "", "proposal": {"minimum": NaN}}',
        b'{"question": "x", "instructions": "", "proposal": {"minimum": 1e999}}',
        "{}".encode("utf-16"),
        b" " * (task_contract_cli.MAX_REQUEST_BYTES + 1),
    ],
    ids=["malformed", "array", "missing", "type", "extra", "nan", "infinity", "utf16", "size"],
)
def test_cli_refuses_invalid_request_envelopes(tmp_path, data):
    path, output = tmp_path / "request.json", tmp_path / "accepted.json"
    path.write_bytes(data)
    with pytest.raises(SystemExit) as caught:
        task_contract_cli.main(["--request", str(path), "--output", str(output)])
    assert caught.value.code == 2 and not output.exists()


def test_standard_output_and_existing_output_file_preservation(tmp_path, capsys):
    before = copy.deepcopy(json.loads(EXAMPLE.read_text()))
    task_contract_cli.main(["--request", str(EXAMPLE)])
    assert json.loads(capsys.readouterr().out)["answer"]["decimals"] == 2
    output = tmp_path / "existing.json"
    output.write_bytes(b"original")
    with pytest.raises(SystemExit) as caught:
        task_contract_cli.main(["--request", str(EXAMPLE), "--output", str(output)])
    assert caught.value.code == 2 and output.read_bytes() == b"original"
    assert json.loads(EXAMPLE.read_text()) == before


def test_cli_unreadable_request_and_unavailable_output_fail_explicitly(tmp_path):
    with pytest.raises(SystemExit) as caught:
        task_contract_cli.main(["--request", str(tmp_path / "missing.json")])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        task_contract_cli.main(
            ["--request", str(EXAMPLE), "--output", str(tmp_path / "missing/accepted.json")]
        )
    assert caught.value.code == 2 and not (tmp_path / "missing").exists()
