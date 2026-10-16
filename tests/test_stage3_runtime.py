import json
import subprocess

from generaldataagent.evaluation.stage_three_runtime import run_stage_three_runtime_security


def test_runtime_security_aggregates_image_and_enforcement_checks(monkeypatch) -> None:
    image = {
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "User": "65532:65532",
            "Entrypoint": ["python", "-I", "-S", "/app/worker.py"],
            "Env": ["PATH=/usr/bin", "PYTHONHASHSEED=0"],
        },
    }
    runtime = {
        "uid_non_root": True,
        "gid_non_root": True,
        "app_write_denied": True,
        "etc_write_denied": True,
        "tmp_writable": True,
        "tmp_noexec_enforced": True,
        "network_default_deny": True,
        "no_new_privileges": True,
        "capabilities_dropped": True,
        "seccomp_filter_enabled": True,
        "pids_limit_enforced": True,
        "memory_limit_enforced": True,
        "cpu_limit_enforced": True,
        "secretless_environment": True,
        "root_mount_read_only": True,
        "tmp_mount_hardened": True,
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = json.dumps(image if command[1:3] == ["image", "inspect"] else runtime)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("generaldataagent.evaluation.stage_three_runtime.subprocess.run", fake_run)
    report = run_stage_three_runtime_security(
        engine="docker",
        image_ref=f"generaldataagent-stage3@sha256:{'a' * 64}",
    )

    assert report["status"] == "passed"
    assert report["total"] >= 20
    assert report["failed"] == 0
    assert "--network=none" in calls[1]
    assert "--read-only" in calls[1]
    assert "--entrypoint=python" in calls[1]
