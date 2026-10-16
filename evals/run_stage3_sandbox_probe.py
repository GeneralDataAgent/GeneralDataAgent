"""Execute the Stage 3 OCI isolation and escape-test probe."""

import argparse
import json

from generaldataagent.evaluation.stage_three_runtime import run_stage_three_runtime_security


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    args = parser.parse_args()
    report = run_stage_three_runtime_security(
        engine=args.engine,
        image_ref=args.image_ref,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
