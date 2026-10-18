#!/usr/bin/env python3
"""Policy-as-code for Ray manifests, without Kyverno or OPA.

Run from the tutorial root:

    python examples/policy_lab.py

The lab evaluates the four Ray CRDs with a small, explicit policy engine:

* required ownership/cost labels;
* pinned images (no ``:latest``);
* CPU/memory requests and GPU request == limit;
* non-root, no privilege escalation, no hostPath;
* autoscaler bounds and name length.

It then runs the same policies in ``enforce`` and ``audit`` modes.  The point
is not to replace Kyverno/Gatekeeper but to make the admission contract
readable, testable, and reviewable in the same pull request as the manifests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "manifests" / "samples"

RAY_KINDS = {"RayCluster", "RayJob", "RayService", "RayCronJob"}


def iter_dicts(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def pod_specs(document: dict) -> list[dict]:
    specs = []
    for item in iter_dicts(document.get("spec") or {}):
        if "containers" in item and isinstance(item.get("containers"), list):
            specs.append(item)
        elif "template" in item and isinstance(item.get("template"), dict):
            template_spec = (item["template"].get("spec") or {})
            if "containers" in template_spec:
                specs.append(template_spec)
    # Deduplicate by identity; the recursive walk sees the same dict twice.
    unique, seen = [], set()
    for spec in specs:
        if id(spec) not in seen:
            seen.add(id(spec))
            unique.append(spec)
    return unique


def containers(document: dict) -> list[dict]:
    result = []
    for spec in pod_specs(document):
        result.extend(spec.get("containers") or [])
        result.extend(spec.get("initContainers") or [])
    return result


def ray_spec(document: dict) -> dict:
    """Return the RayCluster-shaped spec nested in any of the four CRDs."""
    spec = document.get("spec") or {}
    if document.get("kind") == "RayJob":
        return spec.get("rayClusterSpec") or {}
    if document.get("kind") == "RayService":
        return spec.get("rayClusterConfig") or {}
    if document.get("kind") == "RayCronJob":
        job_template = spec.get("jobTemplate") or {}
        return ((job_template.get("spec") or {}).get("rayClusterSpec")) or {}
    return spec


def gpu_value(resources: dict, key: str = "requests") -> float:
    value = (resources.get(key) or {}).get("nvidia.com/gpu")
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


POLICIES = {
    "require-ownership-labels": "Ray CRs carry app.kubernetes.io/part-of, team and cost-center",
    "pin-images": "every image has a tag (not :latest) or a digest",
    "declare-requests": "every container declares CPU and memory requests",
    "gpu-request-equals-limit": "GPU requests equal limits (extended resources are not compressible)",
    "restricted-security": "runAsNonRoot, no privilege escalation, drop ALL capabilities",
    "no-hostpath": "no hostPath volumes",
    "autoscaler-bounds": "minReplicas <= replicas <= maxReplicas",
    "name-length": "RayCluster name <= 53 characters",
}


def violation(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def evaluate(document: dict) -> list[dict]:
    problems: list[dict] = []
    metadata = document.get("metadata") or {}
    kind = document.get("kind")

    if kind in RAY_KINDS:
        labels = metadata.get("labels") or {}
        missing = [key for key in ("app.kubernetes.io/part-of", "team", "cost-center") if key not in labels]
        if missing:
            problems.append(violation("require-ownership-labels", f"missing labels: {', '.join(missing)}"))
        name = str(metadata.get("name") or "")
        if kind == "RayCluster" and len(name) > 53:
            problems.append(violation("name-length", f"name {name!r} is {len(name)} characters (max 53)"))

    for container in containers(document):
        image = str(container.get("image") or "")
        if not image:
            problems.append(violation("pin-images", "container has no image"))
        elif "@sha256:" not in image:
            tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
            if not tag or tag == "latest":
                problems.append(violation("pin-images", f"image {image!r} is not pinned"))
        resources = container.get("resources") or {}
        requests = resources.get("requests") or {}
        if "cpu" not in requests or "memory" not in requests:
            problems.append(violation("declare-requests", f"container {container.get('name')!r} lacks cpu/memory requests"))
        if gpu_value(resources, "requests") != gpu_value(resources, "limits"):
            problems.append(violation("gpu-request-equals-limit", f"container {container.get('name')!r} has GPU mismatch"))
        security = container.get("securityContext") or {}
        if security.get("allowPrivilegeEscalation") is not False:
            problems.append(violation("restricted-security", f"container {container.get('name')!r} allows privilege escalation"))
        capabilities = security.get("capabilities") or {}
        if "ALL" not in (capabilities.get("drop") or []):
            problems.append(violation("restricted-security", f"container {container.get('name')!r} does not drop ALL capabilities"))

    for spec in pod_specs(document):
        pod_security = spec.get("securityContext") or {}
        if pod_security.get("runAsNonRoot") is not True:
            problems.append(violation("restricted-security", "pod does not set runAsNonRoot=true"))
        for volume in spec.get("volumes") or []:
            if "hostPath" in volume:
                problems.append(violation("no-hostpath", f"volume {volume.get('name')!r} uses hostPath"))

    spec = ray_spec(document)
    for group in spec.get("workerGroupSpecs") or []:
        replicas = int(group.get("replicas", 0) or 0)
        minimum = int(group.get("minReplicas", 0) or 0)
        maximum = int(group.get("maxReplicas", 0) or 0)
        if maximum and not (minimum <= replicas <= maximum):
            problems.append(
                violation(
                    "autoscaler-bounds",
                    f"group {group.get('groupName')!r}: min={minimum} replicas={replicas} max={maximum}",
                )
            )
    return problems


def admit(document: dict, mode: str) -> tuple[bool, list[dict]]:
    problems = evaluate(document)
    if mode == "enforce":
        return not problems, problems
    if mode == "audit":
        return True, problems
    raise ValueError(f"unknown mode: {mode}")


def compliant_cluster() -> dict:
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayCluster",
        "metadata": {
            "name": "compliant",
            "namespace": "team-a",
            "labels": {
                "app.kubernetes.io/part-of": "ray-platform",
                "team": "team-a",
                "cost-center": "cc-42",
            },
        },
        "spec": {
            "rayVersion": "2.59.0",
            "enableInTreeAutoscaling": True,
            "headGroupSpec": {
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "securityContext": {"runAsNonRoot": True},
                        "containers": [
                            {
                                "name": "ray",
                                "image": "rayproject/ray:2.59.0",
                                "resources": {"requests": {"cpu": "2", "memory": "4Gi"}},
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                    }
                },
            },
            "workerGroupSpecs": [
                {
                    "groupName": "workers",
                    "replicas": 2,
                    "minReplicas": 1,
                    "maxReplicas": 8,
                    "template": {
                        "spec": {
                            "securityContext": {"runAsNonRoot": True},
                            "containers": [
                                {
                                    "name": "ray",
                                    "image": "rayproject/ray:2.59.0-gpu",
                                    "resources": {
                                        "requests": {"cpu": "8", "memory": "64Gi", "nvidia.com/gpu": "1"},
                                        "limits": {"nvidia.com/gpu": "1"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                }
                            ],
                        }
                    },
                }
            ],
        },
    }


def bad_cluster() -> dict:
    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayCluster",
        "metadata": {"name": "bad", "namespace": "team-b", "labels": {}},
        "spec": {
            "rayVersion": "2.59.0",
            "headGroupSpec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": "rayproject/ray:latest",
                                "securityContext": {"privileged": True},
                            }
                        ],
                        "volumes": [{"name": "docker", "hostPath": {"path": "/var/run/docker.sock"}}],
                    }
                }
            },
            "workerGroupSpecs": [
                {"groupName": "w", "replicas": 9, "minReplicas": 1, "maxReplicas": 4, "template": {"spec": {"containers": []}}}
            ],
        },
    }


def main() -> int:
    documents = []
    for path in sorted(SAMPLES.glob("*.yaml")):
        try:
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if isinstance(document, dict) and document.get("kind") in RAY_KINDS:
                    documents.append((path.name, document))
        except yaml.YAMLError as exc:
            print(f"{path.name}: YAML error: {exc}")

    counts = {code: 0 for code in POLICIES}
    for _, document in documents:
        for problem in evaluate(document):
            counts[problem["code"]] += 1

    print("== policy-as-code lab ==")
    print(f"scanned {len(documents)} Ray CRs in {SAMPLES.name}/")
    print(f"{'policy':<30} {'violations':>10}")
    for code, description in POLICIES.items():
        print(f"{code:<30} {counts[code]:>10}  # {description}")

    good = compliant_cluster()
    assert evaluate(good) == []

    bad = bad_cluster()
    codes = {problem["code"] for problem in evaluate(bad)}
    expected = {
        "require-ownership-labels",
        "pin-images",
        "declare-requests",
        "restricted-security",
        "no-hostpath",
        "autoscaler-bounds",
    }
    assert expected <= codes, codes

    allowed, warnings = admit(good, "enforce")
    denied, deny_reasons = admit(bad, "enforce")
    audited, audit_warnings = admit(bad, "audit")
    assert allowed is True and warnings == []
    assert denied is False and deny_reasons
    assert audited is True and audit_warnings

    print("\n-- admission decisions --")
    print(f"compliant / enforce : allowed={allowed} warnings={len(warnings)}")
    print(f"bad / enforce       : allowed={denied} reasons={len(deny_reasons)}")
    print(f"bad / audit         : allowed={audited} warnings={len(audit_warnings)}")
    print("bad codes           : " + ", ".join(sorted(codes)))

    print("\nOK: the admission contract is explicit, testable and mode-aware.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
