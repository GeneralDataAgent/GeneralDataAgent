"""Explicit local, owner-scoped preference maintenance; default is a reviewable dry run."""

import argparse
import importlib
import json
import os
import re
from pathlib import Path

from generaldataagent.contracts import ActorContext
from generaldataagent.memory.display_preferences import PreferenceConflict
from generaldataagent.memory.preference_lifecycle import (
    PreferenceHistoryMaintenance,
    PreferencePurgePlan,
)
from generaldataagent.orchestration.task_presentation import MetricDisplayPreferenceService
from generaldataagent.security.identity import SQLiteSessionBroker


def load_service(factory: str) -> MetricDisplayPreferenceService:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("trusted_module_function_required")
    module, name = factory.split(":")
    service = getattr(importlib.import_module(module), name)()
    if not isinstance(service, MetricDisplayPreferenceService) or not isinstance(
        service.tasks.broker, SQLiteSessionBroker
    ):
        raise ValueError("durable_local_preference_service_required")
    return service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--factory", required=True, help="Trusted operator module:function")
    parser.add_argument("--data-domain", action="append", required=True)
    parser.add_argument("--after-version", type=int)
    parser.add_argument("--scan-limit", type=int)
    parser.add_argument("--retention-seconds", type=int)
    parser.add_argument(
        "--apply-plan", type=Path, help="Apply exactly this fresh reviewed dry-run JSON"
    )
    args = parser.parse_args(argv)
    selection = (args.after_version, args.scan_limit, args.retention_seconds)
    if not args.development or (
        args.apply_plan is not None and any(v is not None for v in selection)
    ):
        print(json.dumps({"status": "explicit_local_maintenance_mode_required"}))
        return 2
    try:
        service = load_service(args.factory)
        # Bearer credentials are never accepted on argv, copied into plans or printed.
        token = os.environ.get("GDA_PREFERENCE_MAINTENANCE_TOKEN", "")
        identity = service.tasks.broker.authenticate(token)
        actor = ActorContext(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            data_domains=tuple(sorted(set(args.data_domain))),
        )
        guard = service._guard(actor, token)
        maintenance = PreferenceHistoryMaintenance(service.store)
        if args.apply_plan is None:
            result = maintenance.plan(
                actor,
                guard=guard,
                after_version=args.after_version if args.after_version is not None else 0,
                scan_limit=args.scan_limit if args.scan_limit is not None else 50,
                retention_seconds=args.retention_seconds
                if args.retention_seconds is not None
                else 7 * 86400,
            )
        else:
            with args.apply_plan.open("rb") as stream:
                content = stream.read(131073)
            if len(content) > 131072:
                raise ValueError("preference_purge_plan_too_large")
            plan = PreferencePurgePlan.model_validate_json(content)
            result = maintenance.apply(actor, plan, guard=guard)
        print(result.model_dump_json())
        return 0
    except PreferenceConflict:
        print(json.dumps({"status": "preference_maintenance_conflict"}))
        return 3
    except Exception:
        print(json.dumps({"status": "preference_maintenance_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
