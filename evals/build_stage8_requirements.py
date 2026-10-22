"""Export the frozen Python 3.12 scientific closure and lockfile wheel hashes."""

import argparse
import importlib.metadata
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl", "cloudpickle", "narwhals"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("the pinned image requires the frozen Python 3.12 environment")
    lines = ["# Python 3.12 scientific closure from uv.lock; exact versions and wheel hashes."]
    found = set()
    for package in sorted(
        tomllib.loads((ROOT / "uv.lock").read_text())["package"], key=lambda p: p["name"]
    ):
        name = package["name"]
        if name not in NAMES or package["version"] != importlib.metadata.version(name):
            continue
        hashes = sorted({wheel["hash"] for wheel in package.get("wheels", [])})
        if not hashes or name in found:
            raise ValueError("ambiguous or missing wheel lock for " + name)
        found.add(name)
        lines.append(
            name
            + "=="
            + package["version"]
            + " \\\n    "
            + " \\\n    ".join("--hash=" + h for h in hashes)
        )
    if found != NAMES:
        raise ValueError("scientific dependency closure is incomplete")
    content = "\n".join(lines) + "\n"
    destination = ROOT / "infra/sandbox/stage8/requirements.txt"
    if args.check:
        if destination.read_text() != content:
            raise ValueError("scientific requirements differ from frozen Python 3.12 lock")
    else:
        destination.write_text(content)


if __name__ == "__main__":
    main()
