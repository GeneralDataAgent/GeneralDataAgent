"""Run the frozen Stage 2 dbt-manifest lineage matrix."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from generaldataagent.data_engineering import DbtManifestLineageLoader

DATASET = Path(__file__).parent / "datasets" / "stage2-lineage.jsonl"


def run_lineage_evaluation(dataset_path: Path = DATASET) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, str]] = []
    complete_cases = 0
    incomplete_cases = 0
    rejected_cases = 0
    stability_checks = 0
    with TemporaryDirectory(prefix="generaldataagent-lineage-") as directory:
        root = Path(directory)
        for index, case in enumerate(cases):
            manifest = root / f"manifest-{index}.json"
            content = json.dumps(
                case["payload"], sort_keys=True, separators=(",", ":")
            ).encode()
            manifest.write_bytes(content)
            try:
                expected_error = case.get("error")
                if expected_error is not None:
                    try:
                        DbtManifestLineageLoader(manifest).load()
                    except Exception as exc:
                        assert expected_error in str(exc)
                        rejected_cases += 1
                    else:
                        raise AssertionError("manifest was accepted")
                    continue

                first = DbtManifestLineageLoader(manifest).load()
                second = DbtManifestLineageLoader(manifest).load()
                expected = case["expected"]
                actual_assets = [
                    [asset.asset_id, asset.asset_type, list(asset.depends_on)]
                    for asset in first.assets
                ]
                assert first.complete == expected["complete"]
                assert list(first.unresolved_dependencies) == expected["unresolved"]
                assert actual_assets == expected["assets"]
                assert first.manifest_hash == f"sha256:{hashlib.sha256(content).hexdigest()}"
                assert first.manifest_hash == second.manifest_hash
                assert first.assets == second.assets
                assert first.unresolved_dependencies == second.unresolved_dependencies
                stability_checks += 1
                if first.complete:
                    complete_cases += 1
                else:
                    incomplete_cases += 1
            except Exception as exc:
                failures.append(
                    {"case_id": case["case_id"], "error": f"{type(exc).__name__}: {exc}"}
                )
    passed = len(cases) - len(failures)
    return {
        "dataset_size": len(cases),
        "passed": passed,
        "failed": len(failures),
        "pass_rate": passed / len(cases) if cases else 0.0,
        "complete_cases": complete_cases,
        "incomplete_cases": incomplete_cases,
        "rejected_cases": rejected_cases,
        "stability_checks": stability_checks,
        "failures": failures,
    }


if __name__ == "__main__":
    result = run_lineage_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
