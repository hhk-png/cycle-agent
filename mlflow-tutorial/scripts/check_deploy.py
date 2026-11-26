#!/usr/bin/env python
"""Static checks for the reference deployment assets.

This does **not** start Docker.  It catches the failures that are cheap to
catch before a human spends time on a disposable host: unparsable YAML,
missing services, missing files referenced by Nginx, and shell syntax errors
when a POSIX shell is available.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "examples" / "deploy"

REQUIRED_FILES = [
    "Dockerfile",
    "docker-compose.yml",
    "nginx.conf",
    ".env.example",
    "README.md",
    "backup.sh",
    "restore.sh",
]
REQUIRED_SERVICES = {"postgres", "minio", "minio-init", "mlflow", "nginx"}
REQUIRED_MLFLOW_FLAGS = {"--backend-store-uri", "--artifacts-destination", "--serve-artifacts"}


def check_files(errors: list[str]) -> None:
    for name in REQUIRED_FILES:
        if not (DEPLOY / name).is_file():
            errors.append(f"missing examples/deploy/{name}")


def check_compose(errors: list[str]) -> None:
    path = DEPLOY / "docker-compose.yml"
    if not path.is_file():
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"docker-compose.yml is not valid YAML: {exc}")
        return
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        errors.append("docker-compose.yml has no services mapping")
        return
    missing = REQUIRED_SERVICES - set(services)
    if missing:
        errors.append(f"docker-compose.yml missing services: {sorted(missing)}")
    mlflow = services.get("mlflow") or {}
    command = mlflow.get("command", "")
    if isinstance(command, list):
        command = " ".join(str(item) for item in command)
    for flag in REQUIRED_MLFLOW_FLAGS:
        if flag not in str(command):
            errors.append(f"mlflow service command is missing {flag}")
    if "healthcheck" not in (mlflow or {}):
        errors.append("mlflow service has no healthcheck")
    minio_init = services.get("minio-init") or {}
    if not minio_init:
        return
    # The bucket name is interpolated inside the container's shell.  It must
    # be passed explicitly, otherwise a custom MLFLOW_BUCKET only affects the
    # MLflow service and the bucket is never created.
    env = minio_init.get("environment") or {}
    if isinstance(env, list):
        env = {str(item).split("=", 1)[0]: item for item in env}
    if "MLFLOW_BUCKET" not in env:
        errors.append("minio-init must receive MLFLOW_BUCKET in its environment")


def check_nginx(errors: list[str]) -> None:
    path = DEPLOY / "nginx.conf"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for token in ("proxy_pass http://mlflow_backend", "client_max_body_size", "ssl_certificate"):
        if token not in text:
            errors.append(f"nginx.conf missing {token!r}")
    if "X-Forwarded-Proto" not in text:
        errors.append("nginx.conf does not forward X-Forwarded-Proto")


def check_shell(errors: list[str]) -> None:
    bash = shutil.which("bash")
    if not bash:
        return
    for name in ("backup.sh", "restore.sh"):
        path = DEPLOY / name
        if not path.is_file():
            continue
        result = subprocess.run(
            [bash, "-n", str(path)], capture_output=True, text=True
        )
        if result.returncode != 0:
            errors.append(f"{name}: {result.stderr.strip()}")


def main() -> int:
    errors: list[str] = []
    if not DEPLOY.is_dir():
        print("Deployment check FAILED: examples/deploy/ is missing")
        return 1
    check_files(errors)
    check_compose(errors)
    check_nginx(errors)
    check_shell(errors)
    if errors:
        print("Deployment check FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Deployment check passed: compose YAML, Nginx references, shell syntax.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
