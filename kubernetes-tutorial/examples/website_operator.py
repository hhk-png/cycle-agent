"""A small level-triggered Website operator for MiniK8s.

Usage:
    python examples/website_operator.py --server http://127.0.0.1:8080 --token mini-admin-token

The controller is deliberately boring: list, compare desired to actual, submit
idempotent changes, update status, sleep.  That is the whole operator pattern.
"""

from __future__ import annotations

import argparse
import time

from mini_k8s.client import ApiClient
from mini_k8s.errors import ApiError

GROUP = "example.com"
VERSION = "v1"
KIND = "Website"
PLURAL = "websites"


def owner_reference(website: dict) -> dict:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": KIND,
        "name": website["metadata"]["name"],
        "uid": website["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def desired_deployment(website: dict) -> dict:
    name = website["metadata"]["name"]
    namespace = website["metadata"].get("namespace", "default")
    spec = website.get("spec", {})
    replicas = int(spec.get("replicas", 1))
    port = int(spec.get("port", 8080))
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": name, "app.kubernetes.io/managed-by": "website-operator"},
            "ownerReferences": [owner_reference(website)],
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [{
                        "name": "web",
                        "image": spec.get("image", "nginx:1.27"),
                        "ports": [{"containerPort": port}],
                        "readinessProbe": {"httpGet": {"path": "/", "port": port},
                                           "periodSeconds": 1, "failureThreshold": 3},
                        "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}},
                    }]
                },
            },
        },
    }


def desired_service(website: dict) -> dict:
    name = website["metadata"]["name"]
    namespace = website["metadata"].get("namespace", "default")
    port = int(website.get("spec", {}).get("port", 8080))
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": name, "app.kubernetes.io/managed-by": "website-operator"},
            "ownerReferences": [owner_reference(website)],
        },
        "spec": {
            "selector": {"app": name},
            "ports": [{"name": "http", "port": 80, "targetPort": port}],
        },
    }


def ensure(client: ApiClient, obj: dict) -> dict:
    try:
        existing = client.get(obj["apiVersion"], obj["kind"], obj["metadata"]["name"],
                              obj["metadata"].get("namespace"))
    except ApiError as exc:
        if exc.status != 404:
            raise
        existing = None
    if existing and existing.get("spec") == obj.get("spec"):
        return existing
    obj = dict(obj)
    if existing and existing["metadata"].get("ownerReferences"):
        obj["metadata"]["ownerReferences"] = existing["metadata"]["ownerReferences"]
    return client.apply(obj)


def update_status(client: ApiClient, website: dict, deployment: dict) -> None:
    status = {
        "readyReplicas": deployment.get("status", {}).get("readyReplicas", 0),
        "url": f"http://{website['metadata']['name']}.{website['metadata'].get('namespace', 'default')}.svc.cluster.local",
    }
    path = client.path_for(f"{GROUP}/{VERSION}", KIND, website["metadata"].get("namespace", "default"),
                           website["metadata"]["name"], "status")
    client.request("PATCH", path, {"status": status}, content_type="application/merge-patch+json")


def reconcile(client: ApiClient) -> None:
    websites = client.request("GET", f"/apis/{GROUP}/{VERSION}/{PLURAL}").get("items", [])
    for website in websites:
        deployment = ensure(client, desired_deployment(website))
        ensure(client, desired_service(website))
        update_status(client, website, deployment)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="mini-admin-token")
    parser.add_argument("--interval", type=float, default=0.3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    client = ApiClient(args.server, token=args.token)
    while True:
        try:
            reconcile(client)
        except Exception as exc:
            print(f"reconcile error: {exc}")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
