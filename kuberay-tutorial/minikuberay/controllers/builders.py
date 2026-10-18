"""Builders that turn Ray CRs into Pods, Services and auxiliary objects."""

from __future__ import annotations

import json

from .. import constants as C
from ..models.core import (
    container,
    new_endpoint_slice,
    new_network_policy,
    new_pod,
    new_pvc,
    new_secret,
    new_service,
    service_port,
)
from ..models.meta import new_object, owner_reference
from ..models.raycluster import (
    head_fqdn,
    head_service_name,
    headless_service_name,
    pod_labels,
    pod_name,
    serve_service_name,
)
from ..util import deep_copy, env_value, get_ray_container, set_env

RAY_ENV = (
    C.RAY_CLUSTER_NAME,
    C.RAY_CLUSTER_NAMESPACE,
    C.RAY_NODE_TYPE,
    C.RAY_IP,
    C.FQ_RAY_IP,
    C.RAY_PORT,
    C.RAY_ADDRESS,
)


def _flags(params: dict) -> list[str]:
    flags: list[str] = []
    for key, value in (params or {}).items():
        flag = f"--{key}"
        if value is True or str(value).lower() == "true":
            flags.append(flag)
        elif value is False or str(value).lower() == "false":
            continue
        elif value is not None and str(value) != "":
            flags.append(f"{flag}={value}")
    return flags


def _start_params(group: dict) -> dict:
    """Apply KubeRay's top-level ``resources``/``labels`` to ray start flags.

    KubeRay accepts ``workerGroupSpecs[].resources`` and ``labels`` as
    first-class fields and translates them into Ray flags.  Keeping the
    translation here means the generated command is the observable contract.
    """
    params = deep_copy(group.get("rayStartParams") or {})
    resources = group.get("resources") or {}
    mapping = {"cpu": "num-cpus", "gpu": "num-gpus", "memory": "memory"}
    for key, flag in mapping.items():
        if key in resources and flag not in params:
            params[flag] = str(resources[key])
    custom = {key: value for key, value in resources.items() if key not in mapping}
    if custom and "resources" not in params:
        params["resources"] = json.dumps(custom, separators=(",", ":"))
    labels = group.get("labels") or {}
    if labels and "labels" not in params:
        params["labels"] = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    return params


def ray_start_command(cluster: dict, node_type: str, params: dict, head_host: str) -> str:
    flags = _flags(params)
    if node_type == C.NODE_TYPE_HEAD:
        base = ["ray", "start", "--head"]
        if not any(flag.startswith("--port=") for flag in flags):
            base.append(f"--port={C.PORT_GCS}")
        if not any(flag.startswith("--dashboard-host") for flag in flags):
            base.append("--dashboard-host=0.0.0.0")
        if not any(flag.startswith("--metrics-export-port") for flag in flags):
            base.append(f"--metrics-export-port={C.PORT_METRICS}")
        base.extend(flags)
        if not any(flag == "--block" or flag.startswith("--block=") for flag in base):
            base.append("--block")
        return " ".join(base)
    base = ["ray", "start"]
    if not any(flag.startswith("--address") for flag in flags):
        base.append(f"--address={head_host}:{C.PORT_GCS}")
    base.extend(flags)
    if not any(flag == "--block" or flag.startswith("--block=") for flag in base):
        base.append("--block")
    return " ".join(base)


def _base_env(cluster: dict, node_type: str, head_host: str) -> list[dict]:
    from ..models.meta import name as obj_name, namespace as obj_ns

    env = [
        {"name": C.RAY_CLUSTER_NAME, "value": obj_name(cluster)},
        {"name": C.RAY_CLUSTER_NAMESPACE, "value": obj_ns(cluster)},
        {"name": C.RAY_NODE_TYPE, "value": node_type},
        {"name": C.RAY_IP, "value": head_host},
        {"name": C.FQ_RAY_IP, "value": f"{head_host}.{obj_ns(cluster)}.svc.{C.DEFAULT_CLUSTER_DOMAIN}"},
        {"name": C.RAY_PORT, "value": str(C.PORT_GCS)},
        {"name": "RAY_USAGE_STATS_KUBERAY_IN_USE", "value": "1"},
        {"name": "RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE", "value": "1"},
    ]
    if node_type == C.NODE_TYPE_HEAD:
        env.append({"name": C.RAY_ADDRESS, "value": f"127.0.0.1:{C.PORT_GCS}"})
    else:
        env.append({"name": C.RAY_ADDRESS, "value": f"{head_host}:{C.PORT_GCS}"})
    return env


def _inject_env(container_spec: dict, additions: list[dict]) -> None:
    for item in additions:
        if env_value(container_spec, item["name"]) is None:
            container_spec.setdefault("env", []).append(deep_copy(item))


def _ports() -> list[dict]:
    return [
        {"name": C.PORT_NAMES[C.PORT_GCS], "containerPort": C.PORT_GCS, "protocol": "TCP"},
        {"name": C.PORT_NAMES[C.PORT_DASHBOARD], "containerPort": C.PORT_DASHBOARD, "protocol": "TCP"},
        {"name": C.PORT_NAMES[C.PORT_CLIENT], "containerPort": C.PORT_CLIENT, "protocol": "TCP"},
        {"name": C.PORT_NAMES[C.PORT_METRICS], "containerPort": C.PORT_METRICS, "protocol": "TCP"},
        {"name": C.PORT_NAMES[C.PORT_SERVE], "containerPort": C.PORT_SERVE, "protocol": "TCP"},
    ]


def _add_shm(spec: dict, ray_container: dict) -> None:
    volumes = spec.setdefault("volumes", [])
    if not any(volume.get("name") == "shared-mem" for volume in volumes):
        volumes.append({"name": "shared-mem", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}})
    mounts = ray_container.setdefault("volumeMounts", [])
    if not any(mount.get("name") == "shared-mem" for mount in mounts):
        mounts.append({"name": "shared-mem", "mountPath": "/dev/shm"})


def scheduler_name_for(scheduler: str | None) -> str | None:
    return C.BATCH_SCHEDULER_NAMES.get(scheduler or "")


def queue_name_for(cluster: dict, scheduler: str | None) -> str:
    labels = (cluster.get("metadata") or {}).get("labels") or {}
    for key in C.QUEUE_LABELS:
        if labels.get(key):
            return str(labels[key])
    return "default"


def build_podgroup(cluster: dict, scheduler: str | None, *, queue: str | None = None) -> dict | None:
    """Create the gang-scheduling unit used by the configured batch scheduler.

    Volcano and scheduler-plugins both use a PodGroup.  YuniKorn expresses gang
    scheduling through scheduler annotations instead, so no PodGroup is needed.
    """
    if scheduler not in ("volcano", "scheduler-plugins"):
        return None
    spec = cluster.get("spec") or {}
    desired = 1  # head
    for group in spec.get("workerGroupSpecs") or []:
        from ..models.raycluster import group_desired_pods

        desired += group_desired_pods(group)
    if scheduler == "volcano":
        api_version = "scheduling.volcano.sh/v1beta1"
    else:
        api_version = "scheduling.x-k8s.io/v1alpha1"
    return new_object(
        api_version,
        C.KIND_PODGROUP,
        f"{cluster['metadata']['name']}-podgroup",
        cluster["metadata"]["namespace"],
        spec={
            "minMember": desired,
            "queue": queue or queue_name_for(cluster, scheduler),
            **({"schedulerName": C.BATCH_SCHEDULER_NAMES[scheduler]} if scheduler == "scheduler-plugins" else {}),
        },
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"], C.LABEL_APP_NAME: "kuberay"},
        owner_references=[owner_reference(cluster)],
    )


def build_head_pod(cluster: dict, *, start_delay: float | None = None, scheduler_name: str | None = None) -> dict:
    spec = cluster.get("spec") or {}
    head = spec.get("headGroupSpec") or {}
    template = deep_copy(head.get("template") or {})
    container_spec = get_ray_container(template.get("spec") or {})
    if container_spec is None:
        raise ValueError("head template has no container")
    head_name = head_service_name(cluster)
    container_spec["command"] = ["/bin/bash", "-c"]
    container_spec["args"] = [
        "ulimit -n 65536; " + ray_start_command(cluster, C.NODE_TYPE_HEAD, _start_params(head), head_name)
    ]
    container_spec["ports"] = _ports()
    _inject_env(container_spec, _base_env(cluster, C.NODE_TYPE_HEAD, head_name))
    if spec.get("enableInTreeAutoscaling") and (spec.get("autoscalerOptions") or {}).get("version", "v2") == "v2":
        # Ray expects this exact (lowercase) spelling; KubeRay sets it for v2
        # and leaves it unset for v1.
        _inject_env(container_spec, [{"name": C.RAY_ENABLE_AUTOSCALER_V2, "value": "true"}])
    auth = spec.get("authOptions") or {}
    if auth:
        if auth.get("mode", "token") == "token":
            _inject_env(container_spec, [{"name": "RAY_AUTH_MODE", "value": "token"}])
            secret_name = auth.get("secretName") or f"{cluster['metadata']['name']}-auth"
            container_spec.setdefault("env", []).append(
                {
                    "name": "RAY_AUTH_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "auth_token"}},
                }
            )
        if auth.get("enableK8sTokenAuth"):
            _inject_env(container_spec, [{"name": "RAY_ENABLE_K8S_TOKEN_AUTH", "value": "true"}])
            _add_volume(
                template["spec"],
                container_spec,
                "ray-token",
                "/var/run/secrets/ray.io/serviceaccount",
                source={
                    "projected": {
                        "sources": [
                            {
                                "serviceAccountToken": {
                                    "path": "token",
                                    "expirationSeconds": 3600,
                                }
                            }
                        ]
                    }
                },
            )
    if spec.get("gcsFaultToleranceOptions"):
        ft = spec["gcsFaultToleranceOptions"]
        if ft.get("backend", "redis") == "redis":
            _inject_env(container_spec, [{"name": "RAY_REDIS_ADDRESS", "value": ft.get("redisAddress", "")}])
            storage_namespace = ft.get("externalStorageNamespace") or cluster["metadata"].get("uid", "")
            if storage_namespace:
                _inject_env(container_spec, [{"name": C.RAY_EXTERNAL_STORAGE_NS, "value": storage_namespace}])
            if ft.get("redisUsername"):
                container_spec.setdefault("env", []).append({"name": "REDIS_USERNAME", **deep_copy(ft["redisUsername"])})
            if ft.get("redisPassword"):
                container_spec.setdefault("env", []).append({"name": "REDIS_PASSWORD", **deep_copy(ft["redisPassword"])})
        else:
            _inject_env(container_spec, [{"name": "RAY_gcs_storage", "value": "rocksdb"}])
            _inject_env(container_spec, [{"name": "RAY_gcs_storage_path", "value": "/data/gcs"}])
            storage = ft.get("storage") or {}
            claim = storage.get("claimName") or f"{cluster['metadata']['name']}-gcs-pvc"
            _add_volume(template["spec"], container_spec, "gcs-storage", "/data/gcs", source={"persistentVolumeClaim": {"claimName": claim}})
    if spec.get("tlsOptions", {}).get("enabled"):
        _inject_env(
            container_spec,
            [
                {"name": C.RAY_USE_TLS, "value": "1"},
                {"name": C.RAY_TLS_SERVER_CERT, "value": "/etc/ray/tls/tls.crt"},
                {"name": C.RAY_TLS_SERVER_KEY, "value": "/etc/ray/tls/tls.key"},
                {"name": C.RAY_TLS_CA_CERT, "value": "/etc/ray/tls/ca.crt"},
            ],
        )
        _add_volume(
            template["spec"],
            container_spec,
            "ray-tls",
            "/etc/ray/tls",
            source={"secret": {"secretName": f"{cluster['metadata']['name']}-head-tls"}},
        )
    _add_shm(template["spec"], container_spec)

    pod_spec = template.setdefault("spec", {})
    pod_spec.setdefault("restartPolicy", "Never")
    pod_spec.setdefault("terminationGracePeriodSeconds", 30)
    if scheduler_name:
        pod_spec.setdefault("schedulerName", scheduler_name)
    if spec.get("enableInTreeAutoscaling"):
        pod_spec.setdefault("serviceAccountName", f"{cluster['metadata']['name']}-autoscaler")
        autoscaler = build_autoscaler_container(cluster)
        pod_spec.setdefault("containers", []).append(autoscaler)
        volumes = pod_spec.setdefault("volumes", [])
        if not any(volume.get("name") == "ray-logs" for volume in volumes):
            volumes.append({"name": "ray-logs", "emptyDir": {}})
        for item in [container_spec, autoscaler]:
            mounts = item.setdefault("volumeMounts", [])
            if not any(mount.get("name") == "ray-logs" for mount in mounts):
                mounts.append({"name": "ray-logs", "mountPath": "/tmp/ray"})
    if spec.get("historyServerOptions", {}).get("collectorOptions", {}).get("image"):
        pod_spec["containers"].append(build_history_collector(cluster))

    pod_labels_map = pod_labels(
        cluster,
        C.NODE_TYPE_HEAD,
        C.HEAD_GROUP_NAME,
        {**(template.get("metadata") or {}).get("labels", {}), **(head.get("labels") or {})},
    )
    if spec.get("authOptions"):
        pod_labels_map[C.LABEL_SERVE] = "false"
    annotations = deep_copy((template.get("metadata") or {}).get("annotations") or {})
    if start_delay is not None:
        annotations[C.ANNOTATION_SIM_POD_START_DELAY] = str(start_delay)
    return new_pod(
        pod_name(cluster, C.NODE_TYPE_HEAD),
        cluster["metadata"]["namespace"],
        deep_copy(pod_spec),
        labels=pod_labels_map,
        annotations=annotations,
        owner_references=[owner_reference(cluster)],
    )


def _add_volume(spec: dict, container_spec: dict, name: str, mount_path: str, *, source: dict | None = None) -> None:
    volumes = spec.setdefault("volumes", [])
    if not any(volume.get("name") == name for volume in volumes):
        volumes.append({"name": name, **(source or {"emptyDir": {}})})
    mounts = container_spec.setdefault("volumeMounts", [])
    if not any(mount.get("name") == name for mount in mounts):
        mounts.append({"name": name, "mountPath": mount_path})


def build_autoscaler_container(cluster: dict) -> dict:
    spec = cluster.get("spec") or {}
    options = spec.get("autoscalerOptions") or {}
    from ..models.meta import name as obj_name, namespace as obj_ns

    item = container(
        "autoscaler",
        options.get("image") or C.DEFAULT_AUTOSCALER_IMAGE,
        command=["/bin/bash", "-c"],
        args=[
            "ulimit -n 65536; ray kuberay-autoscaler "
            f"--cluster-name {obj_name(cluster)} --cluster-namespace {obj_ns(cluster)}"
        ],
        resources=options.get("resources")
        or {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
        env=[
            {"name": C.RAY_CLUSTER_NAME, "value": obj_name(cluster)},
            {"name": C.RAY_CLUSTER_NAMESPACE, "value": obj_ns(cluster)},
            {"name": "RAY_HEAD_POD_NAME", "value": pod_name(cluster, C.NODE_TYPE_HEAD)},
        ],
    )
    item["imagePullPolicy"] = options.get("imagePullPolicy") or "IfNotPresent"
    return item


def build_history_collector(cluster: dict) -> dict:
    spec = cluster.get("spec") or {}
    options = (spec.get("historyServerOptions") or {}).get("collectorOptions") or {}
    return container(
        "ray-history-collector",
        options.get("image") or C.DEFAULT_COLLECTOR_IMAGE,
        resources=options.get("resources") or {"requests": {"cpu": "100m", "memory": "128Mi"}},
        env=options.get("env") or [],
    )


def build_worker_pod(
    cluster: dict,
    group: dict,
    replica_index: int,
    host_index: int = 0,
    *,
    replica_name: str | None = None,
    scheduler_name: str | None = None,
) -> dict:
    """Build one worker Pod.

    A worker *replica* is the unit the autoscaler and ``replicas`` control.  With
    ``numOfHosts > 1`` one replica spans several Pods (for example a TPU slice);
    ``replica_index`` identifies the replica and ``host_index`` the Pod inside
    it.  KubeRay's multi-host reconciler creates/deletes such Pods as a group.
    """
    spec = cluster.get("spec") or {}
    template = deep_copy(group.get("template") or {})
    container_spec = get_ray_container(template.get("spec") or {})
    if container_spec is None:
        raise ValueError("worker template has no container")
    head_host = head_service_name(cluster)
    container_spec["command"] = ["/bin/bash", "-c"]
    params = _start_params(group)
    container_spec["args"] = ["ulimit -n 65536; " + ray_start_command(cluster, C.NODE_TYPE_WORKER, params, head_host)]
    container_spec["ports"] = _ports()
    _inject_env(container_spec, _base_env(cluster, C.NODE_TYPE_WORKER, head_host))
    auth = spec.get("authOptions") or {}
    if auth:
        if auth.get("mode", "token") == "token":
            _inject_env(container_spec, [{"name": "RAY_AUTH_MODE", "value": "token"}])
            secret_name = auth.get("secretName") or f"{cluster['metadata']['name']}-auth"
            container_spec.setdefault("env", []).append(
                {
                    "name": "RAY_AUTH_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "auth_token"}},
                }
            )
        if auth.get("enableK8sTokenAuth"):
            _inject_env(container_spec, [{"name": "RAY_ENABLE_K8S_TOKEN_AUTH", "value": "true"}])
            _add_volume(
                template.setdefault("spec", {}),
                container_spec,
                "ray-token",
                "/var/run/secrets/ray.io/serviceaccount",
                source={
                    "projected": {
                        "sources": [
                            {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}
                        ]
                    }
                },
            )
    if spec.get("gcsFaultToleranceOptions"):
        _inject_env(container_spec, [{"name": "RAY_gcs_rpc_server_reconnect_timeout_s", "value": "600"}])
    if spec.get("tlsOptions", {}).get("enabled"):
        _inject_env(
            container_spec,
            [
                {"name": C.RAY_USE_TLS, "value": "1"},
                {"name": C.RAY_TLS_SERVER_CERT, "value": "/etc/ray/tls/tls.crt"},
                {"name": C.RAY_TLS_SERVER_KEY, "value": "/etc/ray/tls/tls.key"},
                {"name": C.RAY_TLS_CA_CERT, "value": "/etc/ray/tls/ca.crt"},
            ],
        )
        _add_volume(
            template.setdefault("spec", {}),
            container_spec,
            "ray-tls",
            "/etc/ray/tls",
            source={"secret": {"secretName": f"{cluster['metadata']['name']}-worker-tls"}},
        )
    _add_shm(template.setdefault("spec", {}), container_spec)

    group_name = group.get("groupName", "worker")
    hosts = max(1, int(group.get("numOfHosts", 1) or 1))
    labels = pod_labels(cluster, C.NODE_TYPE_WORKER, group_name, {**(template.get("metadata") or {}).get("labels", {}), **(group.get("labels") or {})})
    labels[C.LABEL_WORKER_REPLICA_INDEX] = str(replica_index)
    labels[C.LABEL_HOST_INDEX] = str(host_index)
    labels[C.LABEL_WORKER_REPLICA_NAME] = replica_name or f"{cluster['metadata']['name']}-{group_name}-{replica_index}"
    if len(group.get("labelRefs") or []) or hosts > 1:
        labels[C.LABEL_HEADLESS] = "true"
    annotations = deep_copy((template.get("metadata") or {}).get("annotations") or {})
    name = pod_name(
        cluster,
        C.NODE_TYPE_WORKER,
        group_name,
        replica_index,
        host_index=host_index if hosts > 1 else None,
    )
    worker_spec = deep_copy(template.get("spec") or {})
    if hosts > 1:
        # Give each host a stable DNS name under the headless Service so the
        # replica can discover its peers.
        worker_spec.setdefault("hostname", name[:63])
        worker_spec.setdefault("subdomain", headless_service_name(cluster))
    if scheduler_name:
        worker_spec.setdefault("schedulerName", scheduler_name)
    return new_pod(
        name,
        cluster["metadata"]["namespace"],
        worker_spec,
        labels=labels,
        annotations=annotations,
        owner_references=[owner_reference(cluster)],
    )


def head_service_ports() -> list[dict]:
    return [
        service_port("client", C.PORT_CLIENT),
        service_port("dashboard", C.PORT_DASHBOARD),
        service_port("gcs-server", C.PORT_GCS),
        service_port("metrics", C.PORT_METRICS),
        service_port("serve", C.PORT_SERVE),
    ]


def build_head_service(cluster: dict) -> dict:
    spec = cluster.get("spec") or {}
    head = spec.get("headGroupSpec") or {}
    selector = {
        C.LABEL_CLUSTER: cluster["metadata"]["name"],
        C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD,
        C.LABEL_IDENTIFIER: f"{cluster['metadata']['name']}-head"[:63],
    }
    custom = head.get("headService") or {}
    custom_annotations = (custom.get("metadata") or {}).get("annotations") or {}
    service = new_service(
        head_service_name(cluster),
        cluster["metadata"]["namespace"],
        selector,
        head_service_ports(),
        service_type=head.get("serviceType") or "ClusterIP",
        cluster_ip="None",
        publish_not_ready=True,
        labels={
            C.LABEL_CLUSTER: cluster["metadata"]["name"],
            C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD,
            C.LABEL_APP_NAME: "kuberay",
            C.LABEL_APP_CREATED_BY: "kuberay-operator",
        },
        annotations=spec.get("headServiceAnnotations") or custom_annotations,
        owner_references=[owner_reference(cluster)],
    )
    # Allow a user-supplied head service to customise ports/type while the
    # controller still owns the selector.
    custom_spec = custom.get("spec") or {}
    if custom_spec.get("ports"):
        # Merge by port name.  Appending the defaults would create duplicate
        # names (dashboard/serve/...) and Kubernetes rejects such a Service.
        merged: dict[str, dict] = {port["name"]: port for port in head_service_ports()}
        for port in custom_spec["ports"]:
            merged[port.get("name") or str(port.get("port"))] = deep_copy(port)
        service["spec"]["ports"] = list(merged.values())
    if custom_spec.get("type"):
        service["spec"]["type"] = custom_spec["type"]
    return service


def build_ingress(cluster: dict) -> dict | None:
    """Create the optional Dashboard Ingress configured by ``enableIngress``."""
    spec = cluster.get("spec") or {}
    head = spec.get("headGroupSpec") or {}
    if not head.get("enableIngress"):
        return None
    options = head.get("ingressOptions") or {}
    name = cluster["metadata"]["name"]
    namespace = cluster["metadata"]["namespace"]
    host = options.get("host") or f"{name}.local"
    path = options.get("path") or "/"
    path_type = options.get("pathType") or "Prefix"
    ingress_spec: dict = {
        "rules": [
            {
                "host": host,
                "http": {
                    "paths": [
                        {
                            "path": path,
                            "pathType": path_type,
                            "backend": {
                                "service": {
                                    "name": head_service_name(cluster),
                                    "port": {"number": C.PORT_DASHBOARD},
                                }
                            },
                        }
                    ]
                },
            }
        ]
    }
    if options.get("ingressClassName"):
        ingress_spec["ingressClassName"] = options["ingressClassName"]
    if options.get("tls"):
        ingress_spec["tls"] = deep_copy(options["tls"])
    return new_object(
        "networking.k8s.io/v1",
        C.KIND_INGRESS,
        f"{name}-ingress",
        namespace,
        spec=ingress_spec,
        labels={C.LABEL_CLUSTER: name, C.LABEL_APP_NAME: "kuberay"},
        annotations=deep_copy(options.get("annotations") or {}),
        owner_references=[owner_reference(cluster)],
    )


def build_serve_service(cluster: dict, *, selector: dict | None = None) -> dict:
    namespace = cluster["metadata"]["namespace"]
    labels = {
        C.LABEL_CLUSTER: cluster["metadata"]["name"],
        C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD,
        C.LABEL_APP_NAME: "kuberay",
        C.LABEL_APP_CREATED_BY: "kuberay-operator",
    }
    return new_service(
        serve_service_name(cluster),
        namespace,
        selector
        or {
            C.LABEL_CLUSTER: cluster["metadata"]["name"],
            C.LABEL_SERVE: "true",
        },
        [service_port("serve", C.PORT_SERVE)],
        labels=labels,
        owner_references=[owner_reference(cluster)],
    )


def build_headless_service(cluster: dict) -> dict:
    return new_service(
        headless_service_name(cluster),
        cluster["metadata"]["namespace"],
        {C.LABEL_CLUSTER: cluster["metadata"]["name"], C.LABEL_HEADLESS: "true"},
        [service_port("gcs-server", C.PORT_GCS)],
        cluster_ip="None",
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
    )


def build_gateway(
    service_name: str,
    namespace: str,
    gateway_class: str,
    *,
    name: str | None = None,
    labels: dict | None = None,
    owner_references: list[dict] | None = None,
) -> dict:
    """Gateway created by KubeRay for incremental RayService upgrades."""
    return new_object(
        "gateway.networking.k8s.io/v1",
        C.KIND_GATEWAY,
        name or f"{service_name}-gateway",
        namespace,
        spec={
            "gatewayClassName": gateway_class,
            "listeners": [
                {
                    "name": "serve",
                    "port": C.PORT_SERVE,
                    "protocol": "HTTP",
                    "allowedRoutes": {"namespaces": {"from": "Same"}},
                }
            ],
        },
        status={"conditions": [{"type": "Programmed", "status": "True", "reason": "Programmed", "message": "simulated Gateway"}]},
        labels=labels or {},
        owner_references=owner_references,
    )


def build_httproute(
    service_name: str,
    namespace: str,
    backend_refs: list[dict],
    *,
    gateway_name: str,
    name: str | None = None,
    labels: dict | None = None,
    owner_references: list[dict] | None = None,
    path_prefix: str = "/",
) -> dict:
    """HTTPRoute with weighted Serve backends.

    Each RayCluster has its own stable Serve Service.  During an incremental
    upgrade the controller rewrites the weights, moving traffic from the active
    cluster to the pending one without touching the client-facing Service.
    """
    return new_object(
        "gateway.networking.k8s.io/v1",
        C.KIND_HTTPROUTE,
        name or f"{service_name}-httproute",
        namespace,
        spec={
            "parentRefs": [{"name": gateway_name, "namespace": namespace}],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": path_prefix}}],
                    "backendRefs": deep_copy(backend_refs),
                }
            ],
        },
        status={
            "parents": [
                {
                    "parentRef": {"name": gateway_name, "namespace": namespace},
                    "conditions": [{"type": "Accepted", "status": "True", "reason": "Accepted"}],
                }
            ]
        },
        labels=labels or {},
        owner_references=owner_references,
    )


def build_endpoint_slice(service: dict, pods: list[dict]) -> dict:
    from ..models.core import service_pods

    selected = service_pods(service, pods)
    ports = [
        {
            "name": port.get("name"),
            "port": port.get("targetPort", port.get("port")),
            "protocol": port.get("protocol", "TCP"),
        }
        for port in (service.get("spec") or {}).get("ports") or []
    ]
    return new_endpoint_slice(
        service["metadata"]["name"],
        service["metadata"]["namespace"],
        service["metadata"]["name"],
        selected,
        ports=ports,
        labels={C.LABEL_APP_NAME: "kuberay"},
        owner_references=[owner_reference(service)],
    )


def build_network_policies(cluster: dict) -> list[dict]:
    spec = cluster.get("spec") or {}
    config = spec.get("networkPolicy")
    if not config:
        return []
    mode = config.get("mode", "DenyAll")
    namespace = cluster["metadata"]["namespace"]
    policies = []
    modes = {
        "DenyAll": ["Ingress", "Egress"],
        "DenyAllIngress": ["Ingress"],
        "DenyAllEgress": ["Egress"],
    }[mode]
    for node_type in (C.NODE_TYPE_HEAD, C.NODE_TYPE_WORKER):
        rules = config.get(node_type) or {}
        policies.append(
            new_network_policy(
                f"{cluster['metadata']['name']}-{node_type}",
                namespace,
                {C.LABEL_CLUSTER: cluster["metadata"]["name"], C.LABEL_NODE_TYPE: node_type},
                policy_types=modes,
                ingress=rules.get("ingressRules") or [],
                egress=rules.get("egressRules") or [],
                labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
                owner_references=[owner_reference(cluster)],
            )
        )
    return policies


def build_gcs_pvc(cluster: dict) -> dict | None:
    spec = cluster.get("spec") or {}
    ft = spec.get("gcsFaultToleranceOptions") or {}
    if ft.get("backend") != "rocksdb":
        return None
    storage = ft.get("storage") or {}
    if storage.get("claimName"):
        return None
    retain = storage.get("deletionPolicy") == "Retain"
    pvc = new_pvc(
        f"{cluster['metadata']['name']}-gcs-pvc",
        cluster["metadata"]["namespace"],
        storage.get("size") or "1Gi",
        storage_class=storage.get("storageClassName"),
        access_modes=storage.get("accessModes") or ["ReadWriteOnce"],
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
        retain=retain,
    )
    if retain:
        pvc["metadata"].setdefault("annotations", {})["minikuberay.io/retain"] = "true"
    return pvc


def build_tls_secret(cluster: dict, node_type: str) -> dict:
    # The real operator delegates certificate issuance to cert-manager.  The
    # model keeps the Secret shape so the Pod volume/env wiring is exercised.
    return new_secret(
        f"{cluster['metadata']['name']}-{node_type}-tls",
        cluster["metadata"]["namespace"],
        {
            "tls.crt": "minikuberay-placeholder-cert",
            "tls.key": "minikuberay-placeholder-key",
            "ca.crt": "minikuberay-placeholder-ca",
        },
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
    )


def build_redis_cleanup_job(cluster: dict) -> dict:
    """Kubernetes Job that removes this cluster's Redis namespace on delete.

    The real operator runs a small Redis client image.  Here the container is
    never executed; a simulation annotation makes the Pod finish after a short
    delay so the finalizer path is observable and testable.
    """
    from ..models.core import container, new_job

    name = f"{cluster['metadata']['name']}-redis-cleanup"
    namespace = cluster["metadata"]["namespace"]
    template = {
        "metadata": {
            "labels": {C.LABEL_CLUSTER: cluster["metadata"]["name"], "job-name": name},
            "annotations": {C.ANNOTATION_SIM_JOB_COMPLETE: "0.05"},
        },
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                container(
                    "redis-cleanup",
                    "redis:7",
                    command=["/bin/sh", "-c"],
                    args=["redis-cli -h \"$REDIS_ADDRESS\" --scan --pattern \"$NAMESPACE:*\" | xargs -r redis-cli DEL"],
                    resources={"requests": {"cpu": "100m", "memory": "128Mi"}},
                )
            ],
        },
    }
    job = new_job(
        name,
        namespace,
        template,
        backoff_limit=1,
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"], C.LABEL_APP_NAME: "kuberay"},
        owner_references=[owner_reference(cluster)],
    )
    return job


def build_auth_secret(cluster: dict) -> dict | None:
    spec = cluster.get("spec") or {}
    auth = spec.get("authOptions") or {}
    if not auth:
        return None
    if auth.get("mode", "token") != "token" or auth.get("secretName"):
        return None
    return new_secret(
        f"{cluster['metadata']['name']}-auth",
        cluster["metadata"]["namespace"],
        {"auth_token": "minikuberay-token"},
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
    )


def build_autoscaler_rbac(cluster: dict) -> list[dict]:
    name = f"{cluster['metadata']['name']}-autoscaler"
    namespace = cluster["metadata"]["namespace"]
    sa = new_object(
        C.CORE_API_VERSION,
        C.KIND_SERVICEACCOUNT,
        name,
        namespace,
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
    )
    role = new_object(
        "rbac.authorization.k8s.io/v1",
        C.KIND_ROLE,
        name,
        namespace,
        spec={
            "rules": [
                {
                    "apiGroups": ["ray.io"],
                    "resources": ["rayclusters", "rayclusters/status"],
                    "verbs": ["get", "list", "watch", "patch", "update"],
                },
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch", "delete"]},
            ]
        },
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
    )
    binding = new_object(
        "rbac.authorization.k8s.io/v1",
        C.KIND_ROLEBINDING,
        name,
        namespace,
        spec={"roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}, "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}]},
        labels={C.LABEL_CLUSTER: cluster["metadata"]["name"]},
        owner_references=[owner_reference(cluster)],
    )
    return [sa, role, binding]
