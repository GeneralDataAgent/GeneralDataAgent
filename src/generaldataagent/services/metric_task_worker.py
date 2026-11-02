"""Operator-owned local metric worker; importing this module starts no process."""

import argparse
import importlib
import json
import re
import signal
from threading import Event

from generaldataagent.orchestration.task_service import MetricTaskService
from generaldataagent.security.identity import SQLiteSessionBroker


def load_service(factory: str) -> MetricTaskService:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("trusted_module_function_required")
    module, name = factory.split(":")
    service = getattr(importlib.import_module(module), name)()
    if not isinstance(service, MetricTaskService) or not isinstance(
        service.broker, SQLiteSessionBroker
    ):
        raise ValueError("durable_local_metric_service_required")
    return service


def poll(service: MetricTaskService) -> int:
    try:
        record = service.run_once()
        status = record.status if record is not None else "idle"
    except Exception:
        # Process managers get fixed metadata, never queries, session digests or exception bodies.
        status = "worker_unavailable"
    print(json.dumps({"status": status}), flush=True)
    return int(status in {"failed", "interrupted", "worker_unavailable"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the opt-in local metric task worker")
    parser.add_argument("--factory", required=True, help="Operator-owned Python module:function")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    if not args.development or not 0.2 <= args.poll_seconds <= 60:
        print(json.dumps({"status": "local_development_configuration_required"}), flush=True)
        return 2
    try:
        service = load_service(args.factory)
    except Exception:
        print(json.dumps({"status": "worker_configuration_unavailable"}), flush=True)
        return 2
    if args.check_config:
        print(json.dumps({"status": "configured", "mode": "local_development"}), flush=True)
        return 0
    if args.once:
        return poll(service)
    stop = Event()
    previous = {}
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda *_: stop.set())
        while not stop.is_set():
            poll(service)
            stop.wait(args.poll_seconds)
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
