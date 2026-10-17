"""Run native PostgreSQL qualification in an isolated temporary Docker container."""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import psycopg

from generaldataagent.evaluation.stage_four_postgres import qualify_postgres

IMAGE = "postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--pytest", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", args.image):
        parser.error("official postgres digest pin required; image must already be present")
    subprocess.run(["docker", "image", "inspect", args.image], check=True, capture_output=True)
    name = "gda-stage4-pg-" + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    created = False
    with tempfile.TemporaryDirectory(prefix="gda-stage4-postgres-") as directory:
        envfile = Path(directory) / "postgres.env"
        envfile.write_text(
            "POSTGRES_PASSWORD=" + password + "\nPOSTGRES_DB=stage4_synthetic\n"
            "POSTGRES_INITDB_ARGS=--encoding=UTF8\nPGDATA=/var/lib/postgresql/data/pgdata\n"
        )
        envfile.chmod(0o600)
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--user",
                    "70:70",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--memory=256m",
                    "--cpus=1",
                    "--pids-limit=100",
                    "--publish",
                    "127.0.0.1::5432",
                    "--env-file",
                    str(envfile),
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,noexec,nosuid,size=128m,mode=1777",
                    "--tmpfs",
                    "/var/run/postgresql:rw,noexec,nosuid,size=8m,mode=1777",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
                    args.image,
                    "-c",
                    "max_connections=10",
                    "-c",
                    "shared_buffers=16MB",
                    "-c",
                    "log_statement=none",
                    "-c",
                    "log_min_error_statement=panic",
                ],
                check=True,
                capture_output=True,
            )
            created = True
            address = subprocess.check_output(
                ["docker", "port", name, "5432/tcp"], text=True
            ).strip()
            host, port_text = address.rsplit(":", 1)
            if host != "127.0.0.1":
                raise RuntimeError("loopback-only database binding required")
            port = int(port_text)
            deadline = time.monotonic() + 40
            while True:
                try:
                    with psycopg.connect(
                        host=host,
                        port=port,
                        dbname="stage4_synthetic",
                        user="postgres",
                        password=password,
                        connect_timeout=1,
                        sslmode="disable",
                    ):
                        break
                except psycopg.Error:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "isolated PostgreSQL startup deadline exceeded"
                        ) from None
                    time.sleep(0.2)
            if args.pytest:
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "--cov=generaldataagent",
                        "--cov-fail-under=85",
                    ],
                    env={
                        **os.environ,
                        "LANGSMITH_TRACING": "false",
                        "GDA_TEST_POSTGRES_PORT": str(port),
                        "GDA_TEST_POSTGRES_PASSWORD": password,
                    },
                    check=False,
                ).returncode
            report = qualify_postgres(
                host=host, port=port, database="stage4_synthetic", admin_password=password
            )
            report["image_ref"] = args.image
            print(json.dumps(report, indent=2))
            return int(report["failed"] != 0)
        finally:
            if created:
                subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
