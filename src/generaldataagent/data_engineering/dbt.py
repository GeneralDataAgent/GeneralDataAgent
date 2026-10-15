"""Pinned dbt adapter for governed development and CI commands."""

import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from generaldataagent.contracts.data_engineering import DbtCompileReport, DbtTestReport

_SAFE_SELECTOR = re.compile(r"^[A-Za-z0-9_.:+-]{1,256}$")


class DbtCompileError(RuntimeError):
    pass


class DbtCompileRunner:
    """Runs constrained dbt compile and selected tests on pinned local paths."""

    def __init__(
        self,
        *,
        project_dir: Path,
        profiles_dir: Path,
        executable: Sequence[str] = ("dbt",),
        allowed_targets: frozenset[str] = frozenset({"dev", "ci"}),
        timeout_seconds: float = 120.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve(strict=True)
        self.profiles_dir = profiles_dir.resolve(strict=True)
        if not (self.project_dir / "dbt_project.yml").is_file():
            raise ValueError("project_dir must contain dbt_project.yml")
        if not executable or any(not part for part in executable):
            raise ValueError("dbt executable command must not be empty")
        if not allowed_targets or any(
            target in {"prod", "production"} for target in allowed_targets
        ):
            raise ValueError("dbt compile targets must exclude production")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.executable = tuple(executable)
        self.allowed_targets = allowed_targets
        self.timeout_seconds = timeout_seconds
        self.environment = {"PATH": os.environ.get("PATH", ""), **dict(environment or {})}

    def compile(
        self,
        *,
        target: str,
        selected_resources: tuple[str, ...] = (),
    ) -> DbtCompileReport:
        if target not in self.allowed_targets:
            raise PermissionError(f"dbt target is not approved for compile: {target}")
        invalid = [item for item in selected_resources if not _SAFE_SELECTOR.fullmatch(item)]
        if invalid:
            raise ValueError(f"invalid dbt selectors: {invalid}")
        command = (
            *self.executable,
            "compile",
            "--project-dir",
            str(self.project_dir),
            "--profiles-dir",
            str(self.profiles_dir),
            "--target",
            target,
            "--no-use-colors",
        )
        if selected_resources:
            command = (*command, "--select", *selected_resources)
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=self.project_dir,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DbtCompileError("configured dbt executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise DbtCompileError(f"dbt compile exceeded {self.timeout_seconds:g} seconds") from exc
        elapsed_ms = (time.monotonic() - started) * 1_000
        reported_command = (
            "dbt",
            "compile",
            "--project-dir",
            "<project_dir>",
            "--profiles-dir",
            "<profiles_dir>",
            "--target",
            target,
            "--no-use-colors",
        )
        if selected_resources:
            reported_command = (*reported_command, "--select", *selected_resources)
        return DbtCompileReport(
            project_name=self.project_dir.name,
            target=target,
            selected_resources=selected_resources,
            command=reported_command,
            exit_code=result.returncode,
            succeeded=result.returncode == 0,
            elapsed_ms=elapsed_ms,
            stdout_tail=self._redact_output(result.stdout)[-20_000:],
            stderr_tail=self._redact_output(result.stderr)[-20_000:],
        )

    def validate_test_request(
        self,
        *,
        target: str,
        selected_resources: tuple[str, ...],
    ) -> None:
        if target not in self.allowed_targets:
            raise PermissionError(f"dbt target is not approved for test: {target}")
        if not selected_resources:
            raise ValueError("dbt test requires at least one selected resource")
        invalid = [item for item in selected_resources if not _SAFE_SELECTOR.fullmatch(item)]
        if invalid:
            raise ValueError(f"invalid dbt selectors: {invalid}")

    def _test_selected(
        self,
        *,
        target: str,
        selected_resources: tuple[str, ...],
    ) -> DbtTestReport:
        """Execute only after the policy gateway consumes an exact approval."""
        self.validate_test_request(target=target, selected_resources=selected_resources)
        command = (
            *self.executable,
            "test",
            "--project-dir",
            str(self.project_dir),
            "--profiles-dir",
            str(self.profiles_dir),
            "--target",
            target,
            "--no-use-colors",
            "--select",
            *selected_resources,
        )
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=self.project_dir,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DbtCompileError("configured dbt executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise DbtCompileError(f"dbt test exceeded {self.timeout_seconds:g} seconds") from exc
        elapsed_ms = (time.monotonic() - started) * 1_000
        reported_command = (
            "dbt",
            "test",
            "--project-dir",
            "<project_dir>",
            "--profiles-dir",
            "<profiles_dir>",
            "--target",
            target,
            "--no-use-colors",
            "--select",
            *selected_resources,
        )
        return DbtTestReport(
            project_name=self.project_dir.name,
            target=target,
            selected_resources=selected_resources,
            command=reported_command,
            exit_code=result.returncode,
            succeeded=result.returncode == 0,
            elapsed_ms=elapsed_ms,
            stdout_tail=self._redact_output(result.stdout)[-20_000:],
            stderr_tail=self._redact_output(result.stderr)[-20_000:],
        )

    def _redact_output(self, value: str) -> str:
        return value.replace(str(self.project_dir), "<project_dir>").replace(
            str(self.profiles_dir),
            "<profiles_dir>",
        )
