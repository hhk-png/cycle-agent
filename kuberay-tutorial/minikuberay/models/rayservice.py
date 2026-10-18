"""Ray Serve configuration parsing and status helpers."""

from __future__ import annotations

from typing import Any

from .. import constants as C
from ..util import canonical_json, stable_hash

try:  # PyYAML is a declared dependency, but keep import errors readable.
    import yaml
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("MiniKubeRay requires PyYAML (pip install pyyaml)") from exc


class ServeConfigError(ValueError):
    pass


def parse_serve_config(config: str | None) -> dict:
    if not config or not config.strip():
        return {"applications": []}
    try:
        parsed = yaml.safe_load(config)
    except yaml.YAMLError as exc:
        raise ServeConfigError(f"serveConfigV2 is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ServeConfigError("serveConfigV2 must decode to a mapping")
    applications = parsed.get("applications")
    if applications is None:
        return {"applications": []}
    if not isinstance(applications, list):
        raise ServeConfigError("serveConfigV2.applications must be a list")
    names = set()
    for index, app in enumerate(applications):
        if not isinstance(app, dict):
            raise ServeConfigError(f"applications[{index}] must be a mapping")
        app_name = app.get("name") or "default"
        if app_name in names:
            raise ServeConfigError(f"duplicate Serve application name: {app_name!r}")
        names.add(app_name)
        if not app.get("import_path") and not app.get("route_prefix"):
            raise ServeConfigError(f"application {app_name!r} needs import_path")
        deployments = app.get("deployments") or []
        if deployments and not isinstance(deployments, list):
            raise ServeConfigError(f"application {app_name!r} deployments must be a list")
        for deployment in deployments:
            if not isinstance(deployment, dict) or not deployment.get("name"):
                raise ServeConfigError(f"application {app_name!r} has a deployment without a name")
    return parsed


def serve_config_hash(config: str | None) -> str:
    try:
        parsed = parse_serve_config(config)
    except ServeConfigError:
        parsed = {"raw": config or ""}
    return stable_hash(parsed, 10)


def application_names(config: str | None) -> list[str]:
    parsed = parse_serve_config(config)
    return [app.get("name") or "default" for app in parsed.get("applications") or []]


def desired_deployment_replicas(config: str | None) -> dict[str, dict[str, int]]:
    parsed = parse_serve_config(config)
    result: dict[str, dict[str, int]] = {}
    for app in parsed.get("applications") or []:
        app_name = app.get("name") or "default"
        deployments: dict[str, int] = {}
        for deployment in app.get("deployments") or []:
            replicas = deployment.get("num_replicas")
            if replicas is None:
                replicas = deployment.get("min_replicas", 1)
            deployments[deployment["name"]] = int(replicas or 0)
        result[app_name] = deployments
    return result


def statuses_for_apps(config: str | None, *, ready: bool, message: str = "") -> dict[str, dict]:
    """Build the subset of Ray's Serve app status that KubeRay stores."""
    parsed = parse_serve_config(config)
    apps: dict[str, dict] = {}
    for app in parsed.get("applications") or []:
        app_name = app.get("name") or "default"
        deployments = {}
        for deployment in app.get("deployments") or []:
            deployments[deployment["name"]] = {
                "status": "HEALTHY" if ready else "UPDATING",
                "message": message,
            }
        apps[app_name] = {
            "status": "RUNNING" if ready else "DEPLOYING",
            "message": message,
            "serveDeploymentStatuses": deployments,
        }
    return apps


def all_apps_ready(statuses: dict[str, dict] | None) -> bool:
    # An empty application set is vacuously ready; otherwise a RayService
    # without serveConfigV2 would get endpoints but never become Ready.
    if statuses is None:
        return False
    return all((app.get("status") == "RUNNING") for app in statuses.values())


def total_serve_replicas(config: str | None) -> int:
    return sum(
        replicas
        for app in desired_deployment_replicas(config).values()
        for replicas in app.values()
    )


def cluster_spec_hash(service: dict) -> str:
    """Hash only the fields that require a *new* RayCluster.

    KubeRay applies ``serveConfigV2`` and ``excludeHeadPodFromServeSvc`` to the
    active cluster in place, and worker replica counts are owned by the
    autoscaler.  Hashing those fields would create a second cluster for changes
    that should not roll the data plane (and would double resource usage).
    """
    from ..util import deep_copy

    spec = service.get("spec") or {}
    cluster = deep_copy(spec.get("rayClusterConfig") or {})
    for group in cluster.get("workerGroupSpecs") or []:
        group.pop("replicas", None)
        group.pop("minReplicas", None)
        group.pop("maxReplicas", None)
        (group.get("scaleStrategy") or {}).pop("workersToDelete", None)
    return stable_hash({"rayClusterConfig": cluster}, 10)


def cluster_name_for_service(service_name: str, revision: str) -> str:
    return f"{service_name}-{revision}"[:53]
