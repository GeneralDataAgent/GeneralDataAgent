"""Bounded subprocess runner for fixed data-science templates."""

import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from generaldataagent.contracts import ExperimentSpec, OCIContainerRuntime, SandboxQuota

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
        payload = _serialize_payload(rows, spec, train_indices, test_indices)
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
        return _validate_template_result(result, spec, len(train_indices), len(test_indices))


class OCIContainerSandbox:
    """Runs the fixed worker from an already-present digest-pinned OCI image."""

    def __init__(self, runtime: OCIContainerRuntime) -> None:
        self.runtime = runtime

    @property
    def runtime_fingerprint(self) -> str:
        return self.runtime.image_ref.rsplit("@", 1)[1]

    def run(
        self,
        *,
        rows: list[dict[str, Any]],
        spec: ExperimentSpec,
        train_indices: list[int],
        test_indices: list[int],
    ) -> dict[str, Any]:
        payload = _serialize_payload(rows, spec, train_indices, test_indices)
        if len(payload) > spec.quota.max_input_bytes:
            raise SandboxError("experiment input exceeds sandbox byte quota")
        command = self.build_command(spec.quota)
        try:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                timeout=spec.quota.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SandboxError(
                f"configured OCI engine is unavailable: {self.runtime.engine}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxError("OCI experiment exceeded sandbox time quota") from exc
        if completed.returncode != 0:
            message = completed.stderr.decode(errors="replace")[:2_000]
            raise SandboxError(f"OCI experiment failed: {message}")
        if len(completed.stdout) > spec.quota.max_output_bytes:
            raise SandboxError("OCI experiment output exceeds sandbox byte quota")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxError("OCI experiment returned invalid JSON") from exc
        return _validate_template_result(result, spec, len(train_indices), len(test_indices))

    def build_command(self, quota: SandboxQuota) -> list[str]:
        """Return the complete immutable OCI invocation without executing it."""

        return [
            self.runtime.engine,
            "run",
            "--rm",
            "--interactive",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--pids-limit={self.runtime.pids_limit}",
            f"--memory={quota.memory_mb}m",
            f"--cpus={self.runtime.cpu_count}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,size={self.runtime.tmpfs_mb}m",
            "--user=65532:65532",
            self.runtime.image_ref,
        ]


def _serialize_payload(
    rows: list[dict[str, Any]],
    spec: ExperimentSpec,
    train_indices: list[int],
    test_indices: list[int],
) -> bytes:
    return json.dumps(
        {
            "rows": rows,
            "spec": spec.model_dump(mode="json", exclude={"quota"}),
            "train_indices": train_indices,
            "test_indices": test_indices,
        },
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _validate_template_result(
    result: object,
    spec: ExperimentSpec,
    expected_train_rows: int,
    expected_test_rows: int,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise SandboxError("experiment template returned an invalid result")
    required = {
        "template_version",
        "train_rows",
        "test_rows",
        "metrics",
        "eda",
        "statistical_checks",
        "error_analysis",
        "subgroups",
        "model",
    }
    if set(result) != required:
        raise SandboxError("experiment template result fields do not match the fixed contract")
    if result["template_version"] != "1.0":
        raise SandboxError("experiment template version does not match the fixed contract")
    if result["train_rows"] != expected_train_rows or result["test_rows"] != expected_test_rows:
        raise SandboxError("experiment template split counts do not match the request")
    expected_metrics = (
        {"accuracy", "precision", "recall", "f1"}
        if spec.problem_type == "binary_classification"
        else {"mae", "rmse", "r2"}
    )
    metrics = result["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != expected_metrics:
        raise SandboxError("experiment template metrics do not match the problem type")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in metrics.values()
    ):
        raise SandboxError("experiment template metrics must be finite numbers")
    if spec.problem_type == "binary_classification" and not all(
        0 <= value <= 1 for value in metrics.values()
    ):
        raise SandboxError("classification metrics must be between zero and one")
    for field in ("eda", "statistical_checks", "error_analysis", "model"):
        if not isinstance(result[field], dict):
            raise SandboxError(f"experiment template field {field} must be an object")
    if not isinstance(result["subgroups"], list):
        raise SandboxError("experiment template field subgroups must be an array")
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
