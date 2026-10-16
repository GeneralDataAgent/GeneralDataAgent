"""Executable security probes for the digest-pinned Stage 3 OCI sandbox."""

import json
import subprocess
from typing import Any

from generaldataagent.contracts import OCIContainerRuntime, SandboxQuota
from generaldataagent.data_science import OCIContainerSandbox

_PROBE_SOURCE = r"""
import json
import os
import socket
import subprocess
from pathlib import Path


def denied_write(path):
    try:
        Path(path).write_text("probe")
    except OSError:
        return True
    return False


def mount_options(target):
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == target:
            return set(fields[3].split(","))
    return set()


status = {}
for line in Path("/proc/self/status").read_text().splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        status[key] = value.strip()

try:
    socket.create_connection(("1.1.1.1", 53), timeout=1)
    network_denied = False
except OSError:
    network_denied = True

tmp_probe = Path("/tmp/stage3-write-probe")
tmp_probe.write_text("ok")
tmp_writable = tmp_probe.read_text() == "ok"
tmp_probe.unlink()

executable = Path("/tmp/stage3-exec-probe")
executable.write_text("#!/bin/sh\nexit 0\n")
executable.chmod(0o700)
try:
    subprocess.run([str(executable)], check=False, capture_output=True)
    tmp_noexec_enforced = False
except PermissionError:
    tmp_noexec_enforced = True
executable.unlink()

root_options = mount_options("/")
tmp_options = mount_options("/tmp")
environment_names = set(os.environ)
sensitive_names = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_SECRET",
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
}

print(json.dumps({
    "uid_non_root": os.getuid() == 65532,
    "gid_non_root": os.getgid() == 65532,
    "app_write_denied": denied_write("/app/stage3-write-probe"),
    "etc_write_denied": denied_write("/etc/stage3-write-probe"),
    "tmp_writable": tmp_writable,
    "tmp_noexec_enforced": tmp_noexec_enforced,
    "network_default_deny": network_denied,
    "no_new_privileges": status.get("NoNewPrivs") == "1",
    "capabilities_dropped": int(status.get("CapEff", "1"), 16) == 0,
    "seccomp_filter_enabled": int(status.get("Seccomp", "0")) == 2,
    "pids_limit_enforced": Path("/sys/fs/cgroup/pids.max").read_text().strip() == "32",
    "memory_limit_enforced": Path("/sys/fs/cgroup/memory.max").read_text().strip() == "268435456",
    "cpu_limit_enforced": Path("/sys/fs/cgroup/cpu.max").read_text().split()[0] == "100000",
    "secretless_environment": environment_names.isdisjoint(sensitive_names),
    "root_mount_read_only": "ro" in root_options,
    "tmp_mount_hardened": {"noexec", "nosuid"}.issubset(tmp_options),
}, sort_keys=True))
"""


def run_stage_three_runtime_security(
    *,
    engine: str,
    image_ref: str,
) -> dict[str, Any]:
    """Run repository-owned escape probes under the exact production restrictions."""

    runtime = OCIContainerRuntime(engine=engine, image_ref=image_ref)
    sandbox = OCIContainerSandbox(runtime)
    production_command = sandbox.build_command(SandboxQuota())
    inspect = _run(
        [
            engine,
            "image",
            "inspect",
            image_ref,
            "--format",
            "{{json .}}",
        ]
    )
    image = json.loads(inspect.stdout)
    entrypoint = image.get("Config", {}).get("Entrypoint")
    image_environment = image.get("Config", {}).get("Env") or []
    sensitive_prefixes = (
        "AWS_ACCESS_KEY_ID=",
        "AWS_SECRET_ACCESS_KEY=",
        "AZURE_CLIENT_SECRET=",
        "DATABASE_URL=",
        "GOOGLE_APPLICATION_CREDENTIALS=",
        "OPENAI_API_KEY=",
        "ANTHROPIC_API_KEY=",
    )
    checks: dict[str, bool] = {
        "digest_pinned_image": "@sha256:" in image_ref,
        "linux_amd64_image": image.get("Os") == "linux" and image.get("Architecture") == "amd64",
        "fixed_non_root_image_user": image.get("Config", {}).get("User") == "65532:65532",
        "fixed_worker_entrypoint": entrypoint == ["python", "-I", "-S", "/app/worker.py"],
        "image_environment_secretless": not any(
            value.startswith(sensitive_prefixes) for value in image_environment
        ),
        "production_pull_never": "--pull=never" in production_command,
        "production_has_no_mount_or_env_input": not any(
            value.startswith(("--mount", "--volume", "--env")) for value in production_command
        ),
        "production_not_privileged": "--privileged" not in production_command,
    }
    image_index = production_command.index(image_ref)
    probe_command = [
        *production_command[:image_index],
        "--entrypoint=python",
        image_ref,
        "-I",
        "-S",
        "-c",
        _PROBE_SOURCE,
    ]
    probe = _run(probe_command)
    runtime_checks = json.loads(probe.stdout)
    if not isinstance(runtime_checks, dict) or not all(
        isinstance(value, bool) for value in runtime_checks.values()
    ):
        raise RuntimeError("Stage 3 runtime probe returned an invalid result")
    checks.update(runtime_checks)
    results = [
        {"check_id": check_id, "passed": passed} for check_id, passed in sorted(checks.items())
    ]
    passed = sum(item["passed"] for item in results)
    return {
        "gate_id": "stage3_oci_runtime_security",
        "status": "passed" if passed == len(results) else "failed",
        "image_ref": image_ref,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "checks": results,
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    if completed.returncode != 0:
        message = completed.stderr.strip()[:2_000]
        raise RuntimeError(f"Stage 3 OCI security probe failed: {message}")
    return completed
