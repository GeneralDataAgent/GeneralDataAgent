"""Explicit local owner-scoped retained-source planning/comparison; never applies a purge."""

import argparse
import importlib
import json
import os
import re
from pathlib import Path

from generaldataagent.contracts import ActorContext
from generaldataagent.datasets.csv_retention import (
    CsvRetentionConflict,
    CsvRetentionInspector,
    CsvRetentionPlan,
)
from generaldataagent.security.identity import SQLiteSessionBroker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--factory", required=True, help="Trusted operator module:function")
    parser.add_argument("--data-domain", action="append", required=True)
    parser.add_argument("--verify-plan", type=Path)
    args = parser.parse_args(argv)
    if not args.development:
        print(json.dumps({"status": "explicit_local_inventory_mode_required"}))
        return 2
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", args.factory):
            raise ValueError("trusted_module_function_required")
        module, name = args.factory.split(":")
        reader = getattr(importlib.import_module(module), name)()
        if not isinstance(reader, CsvRetentionInspector) or not isinstance(
            reader.broker, SQLiteSessionBroker
        ):
            raise ValueError("durable_local_csv_reader_required")
        token = os.environ.get("GDA_CSV_RETENTION_TOKEN", "")
        identity = reader.broker.authenticate(token)
        actor = ActorContext(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            data_domains=tuple(sorted(set(args.data_domain))),
        )
        if args.verify_plan is None:
            result = reader.plan(actor, token)
        else:
            with args.verify_plan.open("rb") as file:
                content = file.read(65537)
            if len(content) > 65536:
                raise ValueError("csv_retention_plan_too_large")
            result = reader.verify(actor, token, CsvRetentionPlan.model_validate_json(content))
        print(result.model_dump_json())
        return 0
    except CsvRetentionConflict:
        print(json.dumps({"status": "csv_retention_plan_conflict"}))
        return 3
    except Exception:
        print(json.dumps({"status": "csv_retention_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
