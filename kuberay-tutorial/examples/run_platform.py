#!/usr/bin/env python3
"""Run MiniKubeRay as a small platform: API server + operator + sample apply.

    python examples/run_platform.py --seconds 5
    # open http://127.0.0.1:8080/dashboard
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from minikuberay.apiserver.server import ApiServer
from minikuberay.controllers.base import Operator, OperatorConfig
from minikuberay.kube.client import HTTPClient
from minikuberay.models.defaults import apply_defaults
from minikuberay import constants as C

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seconds", type=float, default=5.0)
    args = parser.parse_args()

    config = OperatorConfig()
    config.feature_gates["RayCronJob"] = True
    operator = Operator(config=config)
    server = ApiServer(
        operator.store,
        host=args.host,
        port=args.port,
        runtime=operator.runtime,
        metrics=operator.metrics.registry,
        history=operator.history,
    )
    url = server.start()
    operator.start()
    client = HTTPClient(url)
    try:
        for filename in ("ray-cluster.yaml", "ray-job.yaml"):
            path = ROOT / "manifests" / "samples" / filename
            if not path.exists():
                continue
            for document in load(path):
                kind = document.get("kind")
                if kind not in C.RAY_KINDS:
                    continue
                body = apply_defaults(document)
                namespace = body["metadata"].get("namespace", "default")
                try:
                    client.create(kind, body, namespace)
                except Exception:
                    client.update(kind, body)
        print(f"platform running at {url}")
        print(f"dashboard: {url}/dashboard")
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            time.sleep(0.5)
        for cluster in client.list(C.KIND_RAYCLUSTER, "default"):
            print("cluster", cluster["metadata"]["name"], cluster.get("status", {}).get("state"))
        for job in client.list(C.KIND_RAYJOB, "default"):
            print("job", job["metadata"]["name"], job.get("status", {}).get("jobDeploymentStatus"))
    finally:
        operator.stop()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
