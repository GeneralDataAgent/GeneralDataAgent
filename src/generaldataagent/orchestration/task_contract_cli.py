"""Validate an explicit public task proposal locally, without an evaluator or model."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from generaldataagent.orchestration.task_semantics import accept_contract

MAX_REQUEST_BYTES = 1_000_000


def _read_request(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds 1000000 bytes")
    try:
        request = json.loads(raw.decode("utf-8"))
        json.dumps(request, allow_nan=False)
    except (ValueError, UnicodeError):
        raise ValueError("request must be finite UTF-8 JSON") from None
    if (
        not isinstance(request, dict)
        or set(request) != {"question", "instructions", "proposal"}
        or not isinstance(request["question"], str)
        or not isinstance(request["instructions"], str)
        or not isinstance(request["proposal"], dict)
    ):
        raise ValueError(
            "request requires question/instructions strings and a proposal object only"
        )
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, help="Create a new file; defaults to standard output"
    )
    args = parser.parse_args(argv)
    try:
        request = _read_request(args.request)
        spec = accept_contract(
            request["proposal"],
            question=request["question"],
            instructions=request["instructions"],
        )
    except OSError:
        parser.error("unable to read request")
    except ValidationError as exc:
        issues = [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        parser.error("invalid public task contract: " + json.dumps(issues))
    except ValueError as exc:
        parser.error(str(exc))
    output = spec.model_dump_json(indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(output)
    else:
        try:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(output)
        except OSError:
            parser.error("unable to create output; existing files are not overwritten")


if __name__ == "__main__":
    main()
