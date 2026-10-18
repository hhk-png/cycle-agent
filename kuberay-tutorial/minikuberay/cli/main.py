"""``python -m minikuberay`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from .. import constants as C
from ..apiserver.server import ApiServer
from ..clock import ManualClock
from ..controllers.base import DEFAULT_FEATURE_GATES, Operator, OperatorConfig
from ..errors import ApiError
from ..kube.client import HTTPClient, resource_path
from ..models.defaults import apply_defaults
from ..models.meta import name as obj_name, namespace as obj_namespace
from ..models.validation import validate_object
from ..util import age_seconds, human_duration
from ..version import DEFAULT_RAY_VERSION, KUBERAY_VERSION, __version__

KIND_ALIASES = {
    "rc": C.KIND_RAYCLUSTER,
    "raycluster": C.KIND_RAYCLUSTER,
    "rayclusters": C.KIND_RAYCLUSTER,
    "rj": C.KIND_RAYJOB,
    "rayjob": C.KIND_RAYJOB,
    "rayjobs": C.KIND_RAYJOB,
    "rs": C.KIND_RAYSERVICE,
    "rayservice": C.KIND_RAYSERVICE,
    "rayservices": C.KIND_RAYSERVICE,
    "cron": C.KIND_RAYCRONJOB,
    "raycronjob": C.KIND_RAYCRONJOB,
    "raycronjobs": C.KIND_RAYCRONJOB,
    "po": C.KIND_POD,
    "pod": C.KIND_POD,
    "pods": C.KIND_POD,
    "svc": C.KIND_SERVICE,
    "service": C.KIND_SERVICE,
    "services": C.KIND_SERVICE,
    "job": C.KIND_JOB,
    "jobs": C.KIND_JOB,
    "events": C.KIND_EVENT,
    "ingress": C.KIND_INGRESS,
    "ingresses": C.KIND_INGRESS,
    "podgroup": C.KIND_PODGROUP,
    "podgroups": C.KIND_PODGROUP,
    "httproute": C.KIND_HTTPROUTE,
    "httproutes": C.KIND_HTTPROUTE,
    "gateway": C.KIND_GATEWAY,
    "gateways": C.KIND_GATEWAY,
}


def resolve_kind(value: str) -> str:
    return KIND_ALIASES.get(value.lower(), value)


def split_ref(ref: str) -> tuple[str, str]:
    if "/" in ref:
        kind, name = ref.split("/", 1)
        return resolve_kind(kind), name
    return resolve_kind(ref), ""


def make_client(args) -> HTTPClient:
    return HTTPClient(args.server, token=args.token)


def load_documents(paths: list[str]) -> list[dict]:
    import yaml

    documents: list[dict] = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            for document in yaml.safe_load_all(handle):
                if document:
                    documents.append(document)
    return documents


# --------------------------------------------------------------------------- server
def cmd_server(args) -> int:
    config = OperatorConfig(namespace=args.namespace or None)
    config.feature_gates.update(parse_feature_gates(args.feature_gates))
    config.history_enabled = not getattr(args, "no_history", False)
    config.history_path = getattr(args, "history_path", None)
    config.batch_scheduler = getattr(args, "batch_scheduler", "") or ""
    clock = ManualClock() if args.manual_clock else None
    operator = Operator(clock=clock, config=config)
    server = ApiServer(
        operator.store,
        host=args.host,
        port=args.port,
        token=args.token,
        read_only=args.read_only,
        runtime=operator.runtime,
        metrics=operator.metrics.registry,
        history=operator.history,
        tracer=operator.tracer,
    )
    url = server.start()
    if not args.no_operator:
        operator.start()
    print(f"MiniKubeRay {__version__} (upstream API {KUBERAY_VERSION})")
    print(f"API server : {url}")
    print(f"Dashboard  : {url}/dashboard")
    print(f"Metrics    : {url}/metrics")
    print(f"Traces     : {url}/api/traces")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        operator.stop()
        server.stop()
    return 0


def cmd_operator(args) -> int:
    config = OperatorConfig(namespace=args.namespace or None)
    config.feature_gates.update(parse_feature_gates(args.feature_gates))
    config.history_enabled = not getattr(args, "no_history", False)
    config.history_path = getattr(args, "history_path", None)
    config.batch_scheduler = getattr(args, "batch_scheduler", "") or ""
    operator = Operator(config=config)
    print(f"operator started with {len(operator.controllers)} controllers; Ctrl+C to stop")
    operator.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        operator.stop()
    return 0


def parse_feature_gates(value: str | None) -> dict[str, bool]:
    gates = {}
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        key, _, raw = item.partition("=")
        gates[key.strip()] = raw.strip().lower() in ("1", "true", "yes", "on")
    return gates


# ---------------------------------------------------------------------------- apply
def cmd_apply(args) -> int:
    documents = load_documents(args.filenames)
    client = make_client(args)
    for document in documents:
        kind = document.get("kind") or ""
        if kind in C.RAY_KINDS:
            document = apply_defaults(document)
        metadata = document.setdefault("metadata", {})
        namespace = metadata.get("namespace") or args.namespace or "default"
        metadata["namespace"] = namespace
        name = metadata.get("name")
        if not name:
            print(f"error: {kind} document has no metadata.name", file=sys.stderr)
            return 1
        try:
            existing = client.get(kind, name, namespace)
        except ApiError:
            existing = None
        if existing:
            document["metadata"]["resourceVersion"] = existing["metadata"].get("resourceVersion")
            client.update(kind, document)
            print(f"{kind.lower()}/{name} configured")
        else:
            client.create(kind, document, namespace)
            print(f"{kind.lower()}/{name} created")
    return 0


# ------------------------------------------------------------------------------ get
def cmd_get(args) -> int:
    client = make_client(args)
    if getattr(args, "cluster", None) and args.ref in ("nodes", "workergroups"):
        path = (
            f"/apis/ray.io/v1/namespaces/{args.namespace}/rayclusters/"
            f"{args.cluster}/{args.ref}"
        )
        result = client._request("GET", path)
        if args.output == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.ref == "nodes":
            print(f"{'POD':<40} {'TYPE':<8} {'GROUP':<16} {'CPU':<8} {'GPU':<5} {'ALIVE'}")
            for node in result.get("items", []):
                print(
                    f"{node.get('podName', ''):<40} {node.get('nodeType', ''):<8} "
                    f"{node.get('group', ''):<16} {node.get('cpu', 0):<8g} {node.get('gpu', 0):<5g} "
                    f"{node.get('alive')}"
                )
            return 0
        print(f"{'GROUP':<20} {'REPLICAS':<10} {'MIN/MAX':<12} {'HOSTS':<8} {'IMAGE'}")
        for group in result.get("items", []):
            print(
                f"{str(group.get('name')):<20} {str(group.get('replicas')):<10} "
                f"{str(group.get('minReplicas')) + '/' + str(group.get('maxReplicas')):<12} "
                f"{str(group.get('numOfHosts')):<8} {group.get('image', '')}"
            )
        return 0
    kind, ref_name = split_ref(args.ref)
    name = ref_name or args.name
    namespace = None if args.all_namespaces else (args.namespace or "default")
    if name:
        obj = client.get(kind, name, namespace or "default")
        if args.output == "json":
            print(json.dumps(obj, indent=2, ensure_ascii=False))
            return 0
        if args.output == "yaml":
            import yaml

            print(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True))
            return 0
        return print_objects([obj], args.output, kind)
    objects = client.list(kind, namespace)
    return print_objects(objects, args.output, kind)


def print_objects(objects: list[dict], output: str, kind: str) -> int:
    import yaml

    if output == "json":
        print(json.dumps({"apiVersion": "v1", "kind": f"{kind}List", "items": objects}, indent=2, ensure_ascii=False))
        return 0
    if output == "yaml":
        print(yaml.safe_dump_all(objects, sort_keys=False, allow_unicode=True))
        return 0
    if kind == C.KIND_RAYCLUSTER:
        print(f"{'NAME':<30} {'STATE':<10} {'WORKERS':<10} {'CPU':<8} {'GPU':<5} {'AGE'}")
        for obj in objects:
            status = obj.get("status") or {}
            print(
                f"{obj_name(obj):<30} {str(status.get('state') or 'pending'):<10} "
                f"{str(status.get('readyWorkerReplicas', 0)) + '/' + str(status.get('desiredWorkerReplicas', 0)):<10} "
                f"{str(status.get('desiredCPU') or '-'):<8} {str(status.get('desiredGPU') or '0'):<5} "
                f"{human_duration(age_seconds(obj.get('metadata', {}).get('creationTimestamp'), _now()))}"
            )
        return 0
    if kind == C.KIND_RAYJOB:
        print(f"{'NAME':<30} {'JOB':<12} {'DEPLOYMENT':<16} {'CLUSTER':<30} {'AGE'}")
        for obj in objects:
            status = obj.get("status") or {}
            print(
                f"{obj_name(obj):<30} {str(status.get('jobStatus') or '-'):<12} "
                f"{str(status.get('jobDeploymentStatus') or 'New'):<16} {str(status.get('rayClusterName') or '-'):<30} "
                f"{human_duration(age_seconds(obj.get('metadata', {}).get('creationTimestamp'), _now()))}"
            )
        return 0
    if kind == C.KIND_RAYSERVICE:
        print(f"{'NAME':<30} {'STATUS':<10} {'ACTIVE CLUSTER':<30} {'ENDPOINTS':<10} {'AGE'}")
        for obj in objects:
            status = obj.get("status") or {}
            active = (status.get("activeServiceStatus") or {}).get("rayClusterName", "-")
            print(
                f"{obj_name(obj):<30} {str(status.get('serviceStatus') or 'NotReady'):<10} {active:<30} "
                f"{str(status.get('numServeEndpoints', 0)):<10} "
                f"{human_duration(age_seconds(obj.get('metadata', {}).get('creationTimestamp'), _now()))}"
            )
        return 0
    if kind == C.KIND_RAYCRONJOB:
        print(f"{'NAME':<30} {'SCHEDULE':<20} {'SUSPEND':<8} {'LAST SCHEDULE'}")
        for obj in objects:
            status = obj.get("status") or {}
            print(
                f"{obj_name(obj):<30} {str((obj.get('spec') or {}).get('schedule')):<20} "
                f"{str((obj.get('spec') or {}).get('suspend', False)):<8} {status.get('lastScheduleTime', '-')}"
            )
        return 0
    if kind == C.KIND_POD:
        print(f"{'NAME':<34} {'PHASE':<10} {'NODE':<8} {'IP':<16} {'AGE'}")
        for obj in objects:
            status = obj.get("status") or {}
            print(
                f"{obj_name(obj):<34} {str(status.get('phase')):<10} {str((obj.get('spec') or {}).get('nodeName') or '-'):<8} "
                f"{str(status.get('podIP') or '-'):<16} {human_duration(age_seconds(obj.get('metadata', {}).get('creationTimestamp'), _now()))}"
            )
        return 0
    print(f"{'NAME':<40} {'KIND':<18} {'AGE'}")
    for obj in objects:
        print(f"{obj_name(obj):<40} {obj.get('kind', ''):<18} {human_duration(age_seconds(obj.get('metadata', {}).get('creationTimestamp'), _now()))}")
    return 0


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------- describe
def cmd_describe(args) -> int:
    import yaml

    client = make_client(args)
    kind, name = split_ref(args.ref)
    if not name:
        print("usage: describe KIND/NAME", file=sys.stderr)
        return 2
    obj = client.get(kind, name, args.namespace)
    print(f"Name:         {obj_name(obj)}")
    print(f"Namespace:    {obj_namespace(obj)}")
    print(f"Kind:         {obj.get('kind')}")
    print(f"Age:          {human_duration(age_seconds(obj.get('metadata', {}).get('creationTimestamp'), _now()))}")
    labels = obj.get("metadata", {}).get("labels") or {}
    if labels:
        print("Labels:       " + ", ".join(f"{key}={value}" for key, value in sorted(labels.items())))
    print("\nSpec:")
    print(yaml.safe_dump(obj.get("spec") or {}, sort_keys=False, allow_unicode=True))
    print("Status:")
    print(yaml.safe_dump(obj.get("status") or {}, sort_keys=False, allow_unicode=True))
    events = client.list(C.KIND_EVENT, args.namespace)
    matched = [
        event for event in events
        if (event.get("involvedObject") or {}).get("name") == name
        and (event.get("involvedObject") or {}).get("kind") == kind
    ]
    if matched:
        print("Events:")
        for event in matched[-20:]:
            print(f"  {event.get('lastTimestamp', '')}  {event.get('type'):<7} {event.get('reason'):<28} {event.get('message')}")
    return 0


# ----------------------------------------------------------------------------- logs
def cmd_logs(args) -> int:
    client = make_client(args)
    kind, name = split_ref(args.ref)
    if not name:
        print("usage: logs KIND/NAME", file=sys.stderr)
        return 2
    subresource = "logs" if kind == C.KIND_RAYJOB else "log"
    path = resource_path(kind, args.namespace, name, subresource)
    query = urllib.parse.urlencode({"tailLines": str(args.tail)})
    body = client._request("GET", path + "?" + query, raw=True).decode("utf-8", "replace")
    print(body, end="")
    return 0


def cmd_delete(args) -> int:
    client = make_client(args)
    kind, name = split_ref(args.ref)
    if not name:
        print("usage: delete KIND/NAME", file=sys.stderr)
        return 2
    client.delete(kind, name, args.namespace, force=args.force)
    print(f"{kind.lower()}/{name} deleted")
    return 0


def cmd_scale(args) -> int:
    client = make_client(args)
    kind, name = split_ref(args.ref)
    if kind != C.KIND_RAYCLUSTER or not name:
        print("usage: scale raycluster/NAME GROUP=REPLICAS", file=sys.stderr)
        return 2
    cluster = client.get(kind, name, args.namespace)
    assignments = {}
    for item in args.assignments:
        if "=" not in item:
            print(f"invalid assignment: {item}", file=sys.stderr)
            return 2
        group, value = item.split("=", 1)
        try:
            assignments[group] = int(value)
        except ValueError:
            print(f"invalid replica count for {group}: {value}", file=sys.stderr)
            return 2
    known = set()
    for group in cluster.get("spec", {}).get("workerGroupSpecs") or []:
        group_name = group.get("groupName")
        known.add(group_name)
        if group_name in assignments:
            group["replicas"] = assignments[group_name]
    unknown = set(assignments) - known
    if unknown:
        print(f"unknown worker group(s): {', '.join(sorted(unknown))}; known: {', '.join(sorted(known))}", file=sys.stderr)
        return 2
    client.update(kind, cluster)
    print(f"raycluster/{name} scaled: " + ", ".join(f"{key}={value}" for key, value in assignments.items()))
    return 0


def cmd_job(args) -> int:
    client = make_client(args)
    if args.action == "submit":
        if not args.cluster or not args.entrypoint:
            print("usage: job submit NAME --cluster CLUSTER --entrypoint 'python job.py'", file=sys.stderr)
            return 2
        obj = {
            "apiVersion": C.RAY_API_VERSION,
            "kind": C.KIND_RAYJOB,
            "metadata": {"name": args.name, "namespace": args.namespace},
            "spec": {
                "entrypoint": args.entrypoint,
                "submissionMode": "HTTPMode",
                "shutdownAfterJobFinishes": False,
                "clusterSelector": {C.LABEL_CLUSTER: args.cluster},
            },
        }
        created = client.create(C.KIND_RAYJOB, obj, args.namespace)
        print(f"rayjob/{created['metadata']['name']} submitted to {args.cluster}")
        return 0
    if args.action != "stop":
        print("usage: job stop NAME", file=sys.stderr)
        return 2
    path = resource_path(C.KIND_RAYJOB, args.namespace, args.name, "stop")
    result = client._request("POST", path, {})
    print(f"rayjob/{args.name}: {result.get('status', 'stopped')}")
    return 0


def cmd_create(args) -> int:
    client = make_client(args)
    if args.resource != "cluster":
        print("usage: create cluster NAME", file=sys.stderr)
        return 2
    name = args.name
    worker_group = {
        "groupName": "workers",
        "replicas": args.worker_replicas,
        "minReplicas": 0,
        "maxReplicas": max(args.worker_replicas, 1),
        "rayStartParams": {},
        "template": {
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "ray",
                        "image": args.image,
                        "resources": {"requests": {"cpu": args.worker_cpu, "memory": "2Gi"}},
                    }
                ],
            }
        },
    }
    obj = {
        "apiVersion": C.RAY_API_VERSION,
        "kind": C.KIND_RAYCLUSTER,
        "metadata": {"name": name, "namespace": args.namespace},
        "spec": {
            "rayVersion": C.DEFAULT_RAY_VERSION,
            "headGroupSpec": {
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "ray",
                                "image": args.image,
                                "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                            }
                        ],
                    }
                },
            },
            "workerGroupSpecs": [worker_group] if args.worker_replicas else [],
        },
    }
    created = client.create(C.KIND_RAYCLUSTER, obj, args.namespace)
    print(f"raycluster/{created['metadata']['name']} created")
    return 0


def cmd_history(args) -> int:
    """Query the History Server: sessions survive RayCluster deletion."""
    client = make_client(args)
    action = args.action
    ref = args.ref or ""
    if action == "list":
        sessions = client._request("GET", "/api/history/sessions").get("sessions", [])
        print(f"{'CLUSTER':<32} {'NAMESPACE':<16} {'STATE':<10} {'EVENTS':<8} {'DELETED':<24}")
        for session in sessions:
            print(
                f"{session.get('cluster', ''):<32} {session.get('namespace', ''):<16} "
                f"{str(session.get('state') or '-'):<10} {str(session.get('eventCount', 0)):<8} "
                f"{str(session.get('deletedAt') or '-'):<24}"
            )
        return 0
    if "/" not in ref:
        print("usage: history <show|events|replay> NAMESPACE/CLUSTER", file=sys.stderr)
        return 2
    namespace, cluster = ref.split("/", 1)
    base = f"/api/history/sessions/{urllib.parse.quote(namespace)}/{urllib.parse.quote(cluster)}"
    if action == "show":
        session = client._request("GET", base)
        print(json.dumps(session, indent=2, ensure_ascii=False))
        return 0
    if action == "events":
        query = urllib.parse.urlencode({"limit": str(args.limit)})
        events = client._request("GET", base + "/events?" + query).get("events", [])
        for event in events:
            print(f"{event.get('timestamp', '')}  {event.get('type', ''):<7} {event.get('reason', ''):<30} {event.get('message', '')}")
        return 0
    if action == "replay":
        replay = client._request("GET", base + "/replay")
        print(json.dumps(replay, indent=2, ensure_ascii=False))
        return 0
    print(f"unknown history action: {action}", file=sys.stderr)
    return 2


def cmd_traces(args) -> int:
    """Print recent operator traces (or one trace as JSON)."""
    client = make_client(args)
    if args.trace_id:
        trace = client._request("GET", f"/api/traces/{urllib.parse.quote(args.trace_id)}")
        print(json.dumps(trace, indent=2, ensure_ascii=False))
        return 0
    query = urllib.parse.urlencode({"limit": str(args.limit)})
    payload = client._request("GET", "/api/traces?" + query)
    if not payload.get("enabled"):
        print("tracing is disabled on this server")
        return 0
    stats = payload.get("stats") or {}
    print(
        f"spans={stats.get('spans', 0)} errors={stats.get('errors', 0)} "
        f"p50={stats.get('p50_ms', 0)}ms p95={stats.get('p95_ms', 0)}ms p99={stats.get('p99_ms', 0)}ms"
    )
    for trace in payload.get("traces", []):
        print(
            f"{trace['trace_id'][:12]}  {trace['span_count']:>3} spans  "
            f"{trace['error_count']} errors  root={trace['root']}"
        )
        if args.tree:
            from ..tracing import span_tree

            for line in span_tree(trace.get("spans", [])):
                print("  " + line)
    return 0


def _print_state_table(dataset: str, items: list[dict]) -> None:
    if dataset == "nodes":
        print(f"{'STATE':<6} {'TYPE':<7} {'CLUSTER':<24} {'POD':<32} {'CPU':>5} {'GPU':>4}")
        for node in items:
            print(
                f"{node.get('state', ''):<6} {node.get('nodeType', ''):<7} "
                f"{node.get('cluster', ''):<24} {node.get('podName', ''):<32} "
                f"{node.get('cpu', 0):>5g} {node.get('gpu', 0):>4g}"
            )
        return
    if dataset == "jobs":
        print(f"{'STATUS':<10} {'JOB':<24} {'CLUSTER':<24} ENTRYPOINT")
        for job in items:
            print(
                f"{job.get('status', ''):<10} {job.get('jobId', ''):<24} "
                f"{job.get('cluster', ''):<24} {job.get('entrypoint', '')}"
            )
        return
    if dataset == "serve/applications":
        print(f"{'STATUS':<12} {'APPLICATION':<24} {'CLUSTER':<24} {'TRAFFIC':>7} {'TARGET':>6}")
        for app in items:
            print(
                f"{app.get('status', ''):<12} {app.get('name', ''):<24} "
                f"{app.get('cluster', ''):<24} {app.get('trafficRoutedPercent', 0):>7} "
                f"{app.get('targetCapacity', 0):>6}"
            )
        return
    print(f"{'TIME':<22} {'TYPE':<8} {'REASON':<28} OBJECT/MESSAGE")
    for event in items:
        involved = event.get("involvedObject") or {}
        target = f"{involved.get('kind', '')}/{involved.get('name', '')}"
        print(
            f"{event.get('lastTimestamp', ''):<22} {event.get('type', ''):<8} "
            f"{event.get('reason', ''):<28} {target} {event.get('message', '')}"
        )


def cmd_state(args) -> int:
    """Query the Ray-style State API subset exposed by MiniKubeRay."""
    client = make_client(args)
    payload = client.state(
        args.dataset,
        namespace=args.namespace if args.namespace else None,
        cluster=args.cluster,
        filter=args.filter,
        limit=args.limit or None,
    )
    data = (payload.get("data") or {}).get("result") or {}
    items = data.get("result") or []
    if args.output == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    _print_state_table(args.dataset, items)
    print(f"\ntotal={data.get('total', len(items))} shown={len(items)}")
    return 0


def cmd_validate(args) -> int:
    documents = load_documents(args.filenames)
    failed = False
    for document in documents:
        defaulted = apply_defaults(document)
        errors = validate_object(defaulted)
        label = f"{document.get('kind', '?')}/{document.get('metadata', {}).get('name', '?')}"
        if errors:
            failed = True
            print(f"{label}: INVALID")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"{label}: valid")
    return 1 if failed else 0


def cmd_version(args) -> int:
    print(f"minikuberay {__version__} (KubeRay API {KUBERAY_VERSION}, Ray default {DEFAULT_RAY_VERSION})")
    return 0


# ----------------------------------------------------------------------------- demo
def cmd_demo(args) -> int:
    from ..demo import run_demo

    return run_demo(
        fast=args.fast,
        verbose=not args.quiet,
        namespace=args.namespace,
        execute=args.execute,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minikuberay", description="A runnable, readable model of the KubeRay operator")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    server = sub.add_parser("server", help="run the API server and operator")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    server.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    server.add_argument("--read-only", action="store_true")
    server.add_argument("--no-operator", action="store_true")
    server.add_argument("--namespace", default=None)
    server.add_argument("--feature-gates", default="")
    server.add_argument("--history-path", default=None, help="persist History Server sessions to this JSON file")
    server.add_argument("--no-history", action="store_true", help="disable History Server collection")
    server.add_argument("--batch-scheduler", default="", choices=["", *C.BATCH_SCHEDULERS], help="create PodGroups and set schedulerName")
    server.add_argument("--manual-clock", action="store_true", help=argparse.SUPPRESS)
    server.set_defaults(func=cmd_server)

    operator = sub.add_parser("operator", help="run only the in-process operator loop")
    operator.add_argument("--namespace", default=None)
    operator.add_argument("--feature-gates", default="")
    operator.add_argument("--history-path", default=None, help="persist History Server sessions to this JSON file")
    operator.add_argument("--no-history", action="store_true", help="disable History Server collection")
    operator.add_argument("--batch-scheduler", default="", choices=["", *C.BATCH_SCHEDULERS], help="create PodGroups and set schedulerName")
    operator.set_defaults(func=cmd_operator)

    for name, func, help_text in (
        ("apply", cmd_apply, "create or update resources from YAML/JSON files"),
        ("validate", cmd_validate, "validate KubeRay manifests locally"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("-f", "--filenames", nargs="+", required=True)
        cmd.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
        cmd.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
        cmd.add_argument("-n", "--namespace", default="default")
        cmd.set_defaults(func=func)

    get = sub.add_parser("get", help="list resources")
    get.add_argument("ref", help="KIND or KIND/NAME (or nodes/workergroups with -c)")
    get.add_argument("name", nargs="?", default="")
    get.add_argument("-c", "--cluster", default=None, help="cluster name for 'get nodes/workergroups'")
    get.add_argument("-n", "--namespace", default="default")
    get.add_argument("-A", "--all-namespaces", action="store_true")
    get.add_argument("-o", "--output", choices=["table", "json", "yaml"], default="table")
    get.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    get.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    get.set_defaults(func=cmd_get)

    describe = sub.add_parser("describe", help="show one resource, its status and events")
    describe.add_argument("ref", help="KIND/NAME")
    describe.add_argument("-n", "--namespace", default="default")
    describe.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    describe.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    describe.set_defaults(func=cmd_describe)

    logs = sub.add_parser("logs", help="print RayJob or Pod logs")
    logs.add_argument("ref", help="rayjob/NAME or pod/NAME")
    logs.add_argument("-n", "--namespace", default="default")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    logs.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    logs.set_defaults(func=cmd_logs)

    delete = sub.add_parser("delete", help="delete one resource")
    delete.add_argument("ref", help="KIND/NAME")
    delete.add_argument("-n", "--namespace", default="default")
    delete.add_argument("--force", action="store_true")
    delete.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    delete.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    delete.set_defaults(func=cmd_delete)

    scale = sub.add_parser("scale", help="scale a RayCluster worker group")
    scale.add_argument("ref", help="raycluster/NAME")
    scale.add_argument("assignments", nargs="+", metavar="GROUP=REPLICAS")
    scale.add_argument("-n", "--namespace", default="default")
    scale.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    scale.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    scale.set_defaults(func=cmd_scale)

    history = sub.add_parser("history", help="query the History Server (sessions survive cluster deletion)")
    history.add_argument("action", choices=["list", "show", "events", "replay"])
    history.add_argument("ref", nargs="?", default="", help="NAMESPACE/CLUSTER for show/events/replay")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    history.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    history.set_defaults(func=cmd_history)

    create = sub.add_parser("create", help="create a RayCluster (kubectl-ray style)")
    create.add_argument("resource", choices=["cluster"])
    create.add_argument("name")
    create.add_argument("--worker-replicas", type=int, default=2)
    create.add_argument("--worker-cpu", default="2")
    create.add_argument("--image", default=C.DEFAULT_IMAGE)
    create.add_argument("-n", "--namespace", default="default")
    create.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    create.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    create.set_defaults(func=cmd_create)

    job = sub.add_parser("job", help="job operations")
    job.add_argument("action", choices=["stop", "submit"])
    job.add_argument("name")
    job.add_argument("--cluster", default=None, help="target RayCluster for submit")
    job.add_argument("--entrypoint", default="", help="command to run for submit")
    job.add_argument("-n", "--namespace", default="default")
    job.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    job.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    job.set_defaults(func=cmd_job)

    demo = sub.add_parser("demo", help="run the end-to-end in-process demonstration")
    demo.add_argument("--fast", action="store_true", help="no sleeps")
    demo.add_argument("--quiet", action="store_true")
    demo.add_argument("--execute", action="store_true", help="accepted for interface parity; the demo runtime remains simulated")
    demo.add_argument("--namespace", default="default")
    demo.set_defaults(func=cmd_demo)

    state = sub.add_parser("state", help="query the Ray-style State API subset (nodes/jobs/serve/events)")
    state.add_argument("dataset", choices=["nodes", "jobs", "serve/applications", "events"])
    state.add_argument("-c", "--cluster", default=None)
    state.add_argument("-n", "--namespace", default=None)
    state.add_argument("--filter", default="", help="substring filter over the JSON record")
    state.add_argument("--limit", type=int, default=0)
    state.add_argument("-o", "--output", choices=["table", "json"], default="table")
    state.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    state.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    state.set_defaults(func=cmd_state)

    traces = sub.add_parser("traces", help="query recent operator traces")
    traces.add_argument("trace_id", nargs="?", default="", help="print one trace as JSON")
    traces.add_argument("--limit", type=int, default=50)
    traces.add_argument("--tree", action="store_true", help="render spans as a tree")
    traces.add_argument("--server", default=os.environ.get("MINIKUBERAY_SERVER", "http://127.0.0.1:8080"))
    traces.add_argument("--token", default=os.environ.get("MINIKUBERAY_TOKEN"))
    traces.set_defaults(func=cmd_traces)

    version = sub.add_parser("version", help="print version information")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ApiError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
