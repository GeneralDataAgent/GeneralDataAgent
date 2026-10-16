"""Run the frozen Stage 3 matrix through a real digest-pinned OCI runtime."""

import argparse
import tempfile
from pathlib import Path

from generaldataagent.contracts import OCIContainerRuntime
from generaldataagent.data_science import OCIContainerSandbox
from generaldataagent.evaluation.stage_three import run_stage_three_evaluation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).parent / "datasets" / "stage3.jsonl",
    )
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    sandbox = OCIContainerSandbox(OCIContainerRuntime(engine=args.engine, image_ref=args.image_ref))
    if args.artifact_root is not None:
        report = run_stage_three_evaluation(args.dataset, args.artifact_root, sandbox=sandbox)
    else:
        with tempfile.TemporaryDirectory(prefix="gda-stage3-oci-") as directory:
            report = run_stage_three_evaluation(args.dataset, Path(directory), sandbox=sandbox)
    print(report.model_dump_json(indent=2))
    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
