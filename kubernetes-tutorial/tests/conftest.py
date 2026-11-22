from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mini_k8s.cluster import MiniCluster


@pytest.fixture
def cluster():
    instance = MiniCluster(start_api=False)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def api_cluster():
    instance = MiniCluster()
    instance.start()
    try:
        # Give the default ServiceAccount controller and node heartbeat a beat.
        deadline = time.time() + 3
        while time.time() < deadline:
            if instance.get("serviceaccounts", "default", "default") and instance.get("nodes", "mini-node-1").get("status", {}).get("conditions"):
                break
            time.sleep(0.05)
        yield instance
    finally:
        instance.stop()


def deployment(name: str = "web", replicas: int = 2, image: str = "nginx:1.27", **spec_extra) -> dict:
    template_spec = {
        "containers": [{
            "name": "web",
            "image": image,
            "ports": [{"containerPort": 80}],
            "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}},
        }]
    }
    if "template_spec" in spec_extra:
        template_spec.update(spec_extra.pop("template_spec"))
    body = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": template_spec},
        },
    }
    body["spec"].update(spec_extra)
    return body


@pytest.fixture
def deploy():
    return deployment
