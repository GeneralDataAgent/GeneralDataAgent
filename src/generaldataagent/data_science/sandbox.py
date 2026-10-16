"""Bounded subprocess runner for fixed data-science templates."""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from generaldataagent.contracts import ExperimentSpec

_WORKER = Path(__file__).with_name("_worker.py")


class SandboxError(RuntimeError):
    pass


class FixedTemplateSandbox:
    """Runs only the repository-owned worker, never caller-provided Python."""

    @property
    def runtime_fingerprint(self) -> str:
        runtime = f"{sys.implementation.name}:{sys.version}"
        digest = hashlib.sha256(_WORKER.read_bytes() + runtime.encode()).hexdigest()
        return f"sha256:{digest}"

    def run(
        self,
        *,
        rows: list[dict[str, Any]],
        spec: ExperimentSpec,
        train_indices: list[int],
        test_indices: list[int],
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "rows": rows,
                "spec": spec.model_dump(mode="json", exclude={"quota"}),
                "train_indices": train_indices,
                "test_indices": test_indices,
            },
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(payload) > spec.quota.max_input_bytes:
            raise SandboxError("experiment input exceeds sandbox byte quota")
        command = [sys.executable, "-I", "-S", str(_WORKER)]
        with tempfile.TemporaryDirectory(prefix="gda-science-") as directory:
            try:
                completed = subprocess.run(
                    command,
                    input=payload,
                    capture_output=True,
                    cwd=directory,
                    env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"},
                    timeout=spec.quota.timeout_seconds,
                    check=False,
                    preexec_fn=_resource_limiter(spec) if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxError("experiment exceeded sandbox time quota") from exc
        if completed.returncode != 0:
            message = completed.stderr.decode(errors="replace")[:2_000]
            raise SandboxError(f"fixed experiment template failed: {message}")
        if len(completed.stdout) > spec.quota.max_output_bytes:
            raise SandboxError("experiment output exceeds sandbox byte quota")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxError("fixed experiment template returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise SandboxError("fixed experiment template returned an invalid result")
        return result


def _resource_limiter(spec: ExperimentSpec):
    def limit() -> None:
        import resource

        limits = (
            (resource.RLIMIT_CPU, spec.quota.cpu_seconds),
            (resource.RLIMIT_AS, spec.quota.memory_mb * 1024 * 1024),
            (resource.RLIMIT_FSIZE, spec.quota.max_output_bytes),
            (resource.RLIMIT_NOFILE, 32),
        )
        for kind, value in limits:
            try:
                resource.setrlimit(kind, (value, value))
            except (OSError, ValueError):
                continue
        if hasattr(resource, "RLIMIT_NPROC"):
            with contextlib.suppress(OSError, ValueError):
                resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))

    return limit
