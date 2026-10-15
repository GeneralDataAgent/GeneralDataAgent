import json
from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

import pytest

from generaldataagent.contracts import ColumnProfile, DataProfile
from generaldataagent.evaluation import load_external_gate_results

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "qualify_authoritative_profiler.py"


def _profile(connector_id: str, *, row_count: int = 3) -> DataProfile:
    return DataProfile(
        connector_id=connector_id,
        relation="orders",
        row_count=row_count,
        columns=(
            ColumnProfile(
                name="id",
                data_type="INTEGER",
                row_count=row_count,
                null_count=0,
                distinct_count=row_count,
                minimum=1,
                maximum=row_count,
                mean=(row_count + 1) / 2,
            ),
        ),
    )


def _write_profiles(tmp_path: Path, *, authoritative_rows: int = 3) -> tuple[Path, Path]:
    actual = tmp_path / "actual.json"
    authoritative = tmp_path / "authoritative.json"
    actual.write_text(_profile("generaldataagent").model_dump_json())
    authoritative.write_text(
        _profile("independent-profiler", row_count=authoritative_rows).model_dump_json()
    )
    return actual, authoritative


def test_authoritative_profiler_qualifier_emits_passing_evidence(tmp_path) -> None:
    actual, authoritative = _write_profiles(tmp_path)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_authoritative_profiler"]
    now = datetime.now(UTC)

    result = qualify(
        actual_profile_path=actual,
        authoritative_profile_path=authoritative,
        actual_source_system="generaldataagent",
        authoritative_source_system="independent-profiler",
        output_dir=output,
        verifier="data-quality-reviewer",
        environment="staging",
        observed_at=now,
    )

    assert result["passed"] is True
    assert result["agreement_rate"] == 1.0
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["authoritative_profiler"]["status"] == "passed"
    payload = json.loads((output / "authoritative_profiler.evidence.json").read_text())
    assert "actual_profile_hash" in payload
    assert "authoritative_profile_hash" in payload
    assert str(tmp_path) not in json.dumps(payload)


def test_authoritative_profiler_qualifier_emits_failed_evidence(tmp_path) -> None:
    actual, authoritative = _write_profiles(tmp_path, authoritative_rows=4)
    output = tmp_path / "evidence"
    qualify = run_path(RUNNER)["qualify_authoritative_profiler"]
    now = datetime.now(UTC)

    result = qualify(
        actual_profile_path=actual,
        authoritative_profile_path=authoritative,
        actual_source_system="generaldataagent",
        authoritative_source_system="independent-profiler",
        output_dir=output,
        verifier="data-quality-reviewer",
        environment="non_production",
        observed_at=now,
    )

    assert result["passed"] is False
    gates = {gate["gate_id"]: gate for gate in load_external_gate_results(output, now=now)}
    assert gates["authoritative_profiler"]["status"] == "failed"


def test_authoritative_profiler_requires_independent_source(tmp_path) -> None:
    actual, authoritative = _write_profiles(tmp_path)
    qualify = run_path(RUNNER)["qualify_authoritative_profiler"]

    with pytest.raises(ValueError, match="independent source"):
        qualify(
            actual_profile_path=actual,
            authoritative_profile_path=authoritative,
            actual_source_system="same-profiler",
            authoritative_source_system="same-profiler",
            output_dir=tmp_path / "evidence",
            verifier="data-quality-reviewer",
            environment="staging",
        )
