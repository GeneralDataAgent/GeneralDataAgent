import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

from generaldataagent.evaluation import load_external_gate_results

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "qualify_real_dbt_project.py"


def _project(tmp_path: Path, *, fail_test: bool = False) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    profiles = tmp_path / "profiles"
    project.mkdir()
    profiles.mkdir()
    (project / "dbt_project.yml").write_text("name: real_fixture\nversion: 1.0.0\n")
    executable = tmp_path / "fake_dbt.py"
    executable.write_text(
        "import sys\n"
        "command = sys.argv[1]\n"
        f"raise SystemExit(2 if command == 'test' and {fail_test!r} else 0)\n"
    )
    return project, profiles, executable


def test_real_dbt_qualifier_emits_loadable_passed_evidence(tmp_path) -> None:
    project, profiles, executable = _project(tmp_path)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_real_dbt_project"]
    now = datetime.now(UTC)

    result = qualify(
        project_dir=project,
        profiles_dir=profiles,
        target="ci",
        selected_resources=("model.orders",),
        output_dir=output,
        verifier="release-reviewer",
        source_system="fixture-dbt",
        environment="non_production",
        executable=(sys.executable, str(executable)),
        observed_at=now,
    )

    assert result["passed"] is True
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["real_dbt_project"]["status"] == "passed"
    assert sum(gate["status"] == "not_run" for gate in gates.values()) == 4
    payload = json.loads((output / "real_dbt_project.evidence.json").read_text())
    assert payload["compile_report"]["succeeded"] is True
    assert payload["test_report"]["succeeded"] is True
    assert str(tmp_path) not in json.dumps(payload)


def test_real_dbt_qualifier_preserves_failed_test_evidence(tmp_path) -> None:
    project, profiles, executable = _project(tmp_path, fail_test=True)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_real_dbt_project"]
    now = datetime.now(UTC)

    result = qualify(
        project_dir=project,
        profiles_dir=profiles,
        target="ci",
        selected_resources=("model.orders",),
        output_dir=output,
        verifier="release-reviewer",
        source_system="fixture-dbt",
        environment="staging",
        executable=(sys.executable, str(executable)),
        observed_at=now,
    )

    assert result["passed"] is False
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["real_dbt_project"]["status"] == "failed"
