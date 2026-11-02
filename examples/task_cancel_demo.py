"""Synthetic real-HTTP cancellation: request, blocked tool unwind, SSE stop confirmation."""

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


def demonstrate(root: Path) -> dict:
    with demo_service(root) as service:
        entered, release = Event(), Event()
        factory = service.runtime_factory

        def blocked_factory(execution):
            runtime = factory(execution)
            execute = runtime.executor.execute

            def blocked(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("synthetic tool release timed out")
                return execute(*args, **kwargs)

            runtime.executor.execute = blocked
            return runtime

        service.runtime_factory = blocked_factory
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
            "idempotency-key": "cancel-demo",
        }
        app = create_app(
            session_broker=service.broker, task_events=service.events, metric_tasks=service
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", access_log=False)
        )
        results = []
        worker = Thread(target=lambda: results.append(service.run_once()), daemon=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
            serving = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            serving.start()
            try:
                deadline = monotonic() + 5
                while not server.started:
                    if not serving.is_alive() or monotonic() >= deadline:
                        raise RuntimeError("loopback server unavailable")
                    sleep(0.01)
                base = f"http://127.0.0.1:{sock.getsockname()[1]}/v1/analysis/tasks"
                with httpx.Client(timeout=15, trust_env=False, headers=headers) as client:
                    accepted = client.post(
                        base, json={"metric_name": "order_count", "metric_version": "v1"}
                    )
                    accepted.raise_for_status()
                    url = base + "/" + accepted.json()["task_id"]
                    worker.start()
                    assert entered.wait(5)
                    response = client.post(url + "/cancel")
                    assert response.status_code == 202
                    assert response.json()["status"] == "cancel_requested"
                    assert response.json()["cancelled_at"] is None and not results
                    statuses, end = [], None
                    with client.stream("GET", url + "/stream") as stream:
                        stream.raise_for_status()
                        event = None
                        for line in stream.iter_lines():
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                data = json.loads(line[5:])
                                if event == "task":
                                    statuses.append(data["status"])
                                    if len(statuses) == 1:
                                        assert data["status"] == "cancel_requested"
                                        release.set()
                                elif event == "stream_end":
                                    end = data["reason"]
                    worker.join(timeout=5)
                    assert results and results[0].status == "cancelled"
                    assert statuses == ["cancel_requested", "cancelled"] and end == "terminal"
                    assert client.post(url + "/retry").status_code == 409
                    assert client.get(url + "/result").status_code == 409
                    return {
                        "real_http": True,
                        "statuses": statuses,
                        "end": end,
                        "worker_status": results[0].status,
                        "model_calls": 0,
                    }
            finally:
                release.set()
                if worker.ident is not None:
                    worker.join(timeout=5)
                server.should_exit = True
                serving.join(timeout=5)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-task-cancel-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
