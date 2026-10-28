"""Qualify full Stage 7 pairs; never stitch per-case best scores across runs."""

import argparse
import json
from pathlib import Path

from generaldataagent.evaluation.stage_seven_acceptance import qualify_stage7


def main():
    parser = argparse.ArgumentParser()
    for name in ("baselines", "candidates", "historical"):
        parser.add_argument("--" + name, type=Path, nargs=3, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, nargs="+", default=[])
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a fresh report")

    def read(paths):
        return [json.loads(path.read_text()) for path in paths]

    from generaldataagent.artifacts import LocalArtifactStore

    stores = [LocalArtifactStore(path) for path in args.artifact_root]

    def read_bytes(ref):
        for store in stores:
            try:
                return store.read_bytes(ref)
            except FileNotFoundError:
                continue
        raise FileNotFoundError("evaluation receipt artifact is unavailable")

    result = qualify_stage7(
        read(args.baselines),
        read(args.candidates),
        read(args.historical),
        read_bytes=read_bytes if stores else None,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("stage7_development_qualified", "historical_correct_n", "blocked_by")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
