"""Operator-only report worker process entry point with graceful shutdown.

The factory is trusted deployment code, never a request/agent-selected module.
No service is installed or enabled by importing this module.
"""

import argparse
import importlib
import json
import re
import signal
import sys
from threading import Event

from generaldataagent.business.worker import ReportWorker
from generaldataagent.security.workload import OIDCWorkloadSessionIssuer, WorkloadSessionBroker


def load_worker(factory: str, *, development: bool = False) -> ReportWorker:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("trusted module:function factory required")
    module, name = factory.split(":")
    worker = getattr(importlib.import_module(module), name)()
    if not isinstance(worker, ReportWorker):
        raise ValueError("factory must return a ReportWorker")
    if not development and (
        not isinstance(worker.token_provider, OIDCWorkloadSessionIssuer)
        or not isinstance(worker.scheduler.actions.broker, WorkloadSessionBroker)
        or worker.token_provider.broker is not worker.scheduler.actions.broker
    ):
        raise ValueError(
            "service requires pinned workload issuer and schedule-constrained shared sessions"
        )
    return worker


def summarize(results: list[dict]) -> None:
    # The process manager receives counts, never credentials, queries, payloads or exception text.
    counts = {"active": 0, "complete": 0, "failed": 0}
    for result in results:
        state = result.get("state")
        if state not in counts:
            raise ValueError("invalid worker status")
        counts[state] += 1
    print(json.dumps({"state": "poll_complete", "jobs": len(results), **counts}), flush=True)


def run_worker(worker: ReportWorker, *, once: bool = False) -> int:
    if once:
        results = worker.run_once()
        summarize(results)
        return int(any(result["state"] == "failed" for result in results))
    stop = Event()
    previous = {}
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda *_: stop.set())
        worker.run(stop, on_result=summarize)
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a trusted report-worker factory")
    parser.add_argument("--factory", required=True, help="Operator-owned Python module:function")
    parser.add_argument(
        "--check-config", action="store_true", help="Load/check wiring without polling"
    )
    parser.add_argument("--once", action="store_true", help="Run one bounded polling batch")
    parser.add_argument(
        "--development", action="store_true", help="Allow isolated local test identities"
    )
    args = parser.parse_args(argv)
    try:
        worker = load_worker(args.factory, development=args.development)
        if args.check_config:
            print(
                json.dumps(
                    {
                        "state": "configured",
                        "mode": "development" if args.development else "workload",
                    }
                )
            )
        else:
            return run_worker(worker, once=args.once)
        return 0
    except Exception:
        print('{"state":"failed","error":"worker_service_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
