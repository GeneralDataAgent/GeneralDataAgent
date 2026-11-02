"""Real loopback HTTP SSE receives queued status before a synthetic worker is released."""

import argparse
import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep

import httpx
import uvicorn

from examples.task_submission_demo import demo_service
from generaldataagent.api.app import create_app
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.orchestration.task_streams import MetricTaskStreams, TaskStreamLimits


def demonstrate(root: Path, *, disconnect_early: bool = False) -> dict:
    with demo_service(root) as service:
        owner = ActorContext(
            user_id="demo", tenant_id="demo", project_id="demo", data_domains=("commerce",)
        )
        token = service.broker.issue(
            user_id="demo",
            tenant_id="demo",
            project_id="demo",
            data_domains=frozenset({"commerce"}),
            permissions=frozenset({"analysis:run"}),
            ttl_seconds=900,
        )
        headers = {
            "authorization": f"Bearer {token}",
            "x-user-id": "demo",
            "x-tenant-id": "demo",
            "x-project-id": "demo",
            "x-data-domain": "commerce",
        }
        record = service.submit(
            MetricQuery(metric_name="order_count", metric_version="v1"), owner, token, "stream-demo"
        )
        limits = TaskStreamLimits(max_seconds=10, poll_seconds=0.02)
        streams = MetricTaskStreams(service, limits=limits)
        app = create_app(
            session_broker=service.broker,
            task_events=service.events,
            metric_tasks=service,
            task_streams=streams,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", access_log=False)
        )
        start_worker = Event()
        worker_results = []

        def work():
            if start_worker.wait(10):
                worker_results.append(service.run_once())

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
            server_thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            worker_thread = Thread(target=work, daemon=True)
            server_thread.start()
            worker_thread.start()
            try:
                deadline = monotonic() + 5
                while not server.started:
                    if not server_thread.is_alive() or monotonic() >= deadline:
                        raise RuntimeError("loopback server unavailable")
                    sleep(0.01)
                frames = []
                url = f"http://127.0.0.1:{sock.getsockname()[1]}/v1/analysis/tasks/{record.task_id}/stream"
                with (
                    httpx.Client(timeout=15, trust_env=False) as client,
                    client.stream("GET", url, headers=headers) as response,
                ):
                    response.raise_for_status()
                    current = {}
                    for line in response.iter_lines():
                        if line.startswith(":"):
                            continue
                        if line:
                            key, value = line.split(":", 1)
                            current[key] = value.lstrip()
                        elif current:
                            current["data"] = json.loads(current["data"])
                            frames.append(current)
                            if len(frames) == 1:
                                assert (
                                    current["event"] == "task"
                                    and current["data"]["status"] == "queued"
                                )
                                assert not worker_results
                                start_worker.set()
                                if disconnect_early:
                                    break
                            current = {}
                worker_thread.join(timeout=5)
                assert worker_results and worker_results[0].status == "completed"
                progress = [frame for frame in frames if frame["event"] == "progress"]
                if not disconnect_early:
                    assert frames[-1]["data"]["reason"] == "terminal"
                deadline = monotonic() + 5
                while streams._connections and monotonic() < deadline:
                    sleep(0.01)
                assert not streams._connections
                return {
                    "first_status": frames[0]["data"]["status"],
                    "worker_status": worker_results[0].status,
                    "progress_sequences": [frame["data"]["sequence"] for frame in progress],
                    "end": frames[-1]["data"],
                    "real_http": True,
                    "model_calls": 0,
                    "disconnected_early": disconnect_early,
                    "active_connections": len(streams._connections),
                }
            finally:
                start_worker.set()
                worker_thread.join(timeout=5)
                server.should_exit = True
                server_thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synthetic loopback task SSE client")
    parser.add_argument("--disconnect", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="gda-task-stream-") as directory:
        print(json.dumps(demonstrate(Path(directory), disconnect_early=args.disconnect), indent=2))
