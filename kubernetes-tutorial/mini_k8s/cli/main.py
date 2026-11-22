"""A kubectl-style CLI for MiniK8s."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from mini_k8s.client import ApiClient
from mini_k8s.errors import ApiError

ALL_RESOURCES = ["pods", "services", "deployments", "replicasets", "statefulsets", "daemonsets", "jobs", "cronjobs"]


def common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server", default=argparse.SUPPRESS,
                        help="API server URL (default: $MINIK8S_SERVER or http://127.0.0.1:8080)")
    parser.add_argument("--token", default=argparse.SUPPRESS,
                        help="bearer token (default: $MINIK8S_TOKEN or the development admin token)")
    parser.add_argument("-n", "--namespace", default=argparse.SUPPRESS, help="namespace")
    parser.add_argument("-A", "--all-namespaces", action="store_true", default=argparse.SUPPRESS,
                        help="query all namespaces")
    parser.add_argument("-o", "--output", default=argparse.SUPPRESS,
                        choices=["table", "wide", "json", "yaml", "name"], help="output format")
    parser.add_argument("-l", "--selector", default=argparse.SUPPRESS, help="label selector")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = common_parser()
    root = argparse.ArgumentParser(prog="mk8s", description="MiniK8s kubectl-compatible CLI")
    root.add_argument("--server", default=os.environ.get("MINIK8S_SERVER", "http://127.0.0.1:8080"))
    root.add_argument("--token", default=os.environ.get("MINIK8S_TOKEN", "mini-admin-token"))
    root.add_argument("-n", "--namespace", default="default")
    root.add_argument("-A", "--all-namespaces", action="store_true")
    root.add_argument("-o", "--output", default="table", choices=["table", "wide", "json", "yaml", "name"])
    root.add_argument("-l", "--selector", default=None)
    sub = root.add_subparsers(dest="subcommand", required=True)

    p = sub.add_parser("apply", parents=[common], help="apply a manifest")
    p.add_argument("-f", "--filename", required=True, help="file, directory or - for stdin")
    p.add_argument("--server-side", action="store_true",
                   help="use server-side apply and field ownership")
    p.add_argument("--field-manager", default="mk8s-client", help="field manager name for server-side apply")
    p.add_argument("--force-conflicts", action="store_true",
                   help="take ownership of fields owned by another manager")

    p = sub.add_parser("create", parents=[common], help="create a manifest")
    p.add_argument("-f", "--filename", required=True)

    p = sub.add_parser("get", parents=[common], help="list resources")
    p.add_argument("resource")
    p.add_argument("names", nargs="*")

    p = sub.add_parser("describe", parents=[common], help="show details")
    p.add_argument("resource")
    p.add_argument("name")

    p = sub.add_parser("delete", parents=[common], help="delete resources")
    p.add_argument("resource", nargs="?")
    p.add_argument("names", nargs="*")
    p.add_argument("-f", "--filename")
    p.add_argument("--all", action="store_true", help="delete every object of the type")

    p = sub.add_parser("logs", parents=[common], help="print container logs")
    p.add_argument("pod")
    p.add_argument("-c", "--container")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--previous", action="store_true")
    p.add_argument("-f", "--follow", action="store_true")

    p = sub.add_parser("exec", parents=[common], help="execute a command in a container")
    p.add_argument("pod")
    p.add_argument("-c", "--container")
    p.add_argument("command_args", nargs=argparse.REMAINDER)

    p = sub.add_parser("set", parents=[common], help="update a workload field")
    p.add_argument("action", choices=["image"])
    p.add_argument("target", help="resource/name, e.g. deployment/web")
    p.add_argument("assignment", help="container=image")

    p = sub.add_parser("scale", parents=[common], help="scale a workload")
    p.add_argument("resource")
    p.add_argument("name")
    p.add_argument("--replicas", type=int, required=True)

    p = sub.add_parser("rollout", parents=[common], help="manage rollouts")
    p.add_argument("action", choices=["status", "history", "undo", "restart"])
    p.add_argument("resource")
    p.add_argument("name")
    p.add_argument("--to-revision", type=int)
    p.add_argument("--timeout", type=float, default=30.0)

    p = sub.add_parser("expose", parents=[common], help="expose a deployment as a service")
    p.add_argument("deployment")
    p.add_argument("--name")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--target-port", type=int)
    p.add_argument("--type", default="ClusterIP", choices=["ClusterIP", "NodePort", "LoadBalancer"])

    p = sub.add_parser("run", parents=[common], help="run an image")
    p.add_argument("name")
    p.add_argument("--image", required=True)
    p.add_argument("--port", type=int)
    p.add_argument("--replicas", type=int, default=1)
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--command-args", dest="command_args", nargs=argparse.REMAINDER)

    p = sub.add_parser("cordon", parents=[common], help="mark a node unschedulable")
    p.add_argument("node")
    p = sub.add_parser("uncordon", parents=[common], help="mark a node schedulable")
    p.add_argument("node")

    p = sub.add_parser("drain", parents=[common], help="evict pods from a node")
    p.add_argument("node")
    p.add_argument("--force", action="store_true")
    p.add_argument("--ignore-daemonsets", action="store_true")

    p = sub.add_parser("taint", parents=[common], help="update node taints")
    p.add_argument("node")
    p.add_argument("taint")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("auth", parents=[common], help="authorization checks")
    p.add_argument("action", choices=["can-i"])
    p.add_argument("verb")
    p.add_argument("resource")

    p = sub.add_parser("api-resources", parents=[common], help="list API resources")
    p.add_argument("--api-group")

    p = sub.add_parser("explain", parents=[common], help="explain a resource")
    p.add_argument("resource")

    p = sub.add_parser("top", parents=[common], help="show resource usage")
    p.add_argument("what", choices=["pods", "nodes"])

    sub.add_parser("cluster-info", parents=[common], help="show cluster endpoints")
    sub.add_parser("version", parents=[common], help="show client and server versions")
    p = sub.add_parser("wait", parents=[common], help="wait for a condition")
    p.add_argument("--for", dest="condition", required=True)
    p.add_argument("resource")
    p.add_argument("name")
    p.add_argument("--timeout", type=float, default=30.0)
    return root


class CLI:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = ApiClient(args.server, token=args.token)
        self.namespace = args.namespace
        self.all_namespaces = args.all_namespaces
        self.output = args.output

    def run(self) -> int:
        handler = getattr(self, f"cmd_{self.args.subcommand.replace('-', '_')}", None)
        if handler is None:
            print(f"unsupported command: {self.args.subcommand}", file=sys.stderr)
            return 2
        try:
            return handler() or 0
        except ApiError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
        except BrokenPipeError:
            return 0

    # ------------------------------------------------------------- discovery
    def resolve(self, token: str) -> tuple[str, dict]:
        for (group, version), resources in self.client.discover().items():
            for kind, info in resources.items():
                names = {info["name"], info.get("singularName", ""), kind.lower(), *info.get("shortNames", [])}
                if token.lower() in {name.lower() for name in names if name}:
                    api_version = f"{group}/{version}" if group else version
                    return api_version, info
        raise ApiError(404, "NotFound", f"the server doesn't have a resource type {token!r}")

    def _namespace(self) -> str | None:
        return None if self.all_namespaces else self.namespace

    # ---------------------------------------------------------------- printing
    def _print(self, obj: Any, *, resource: str = "") -> None:
        if self.output == "json":
            print(json.dumps(obj, indent=2, ensure_ascii=False))
        elif self.output == "yaml":
            print(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True), end="")
        elif self.output == "name":
            if "items" in obj:
                for item in obj["items"]:
                    print(f'{item.get("kind", "").lower()}/{item["metadata"]["name"]}')
            else:
                print(f'{obj.get("kind", "").lower()}/{obj["metadata"]["name"]}')
        else:
            self._print_table(obj, resource)

    def _print_table(self, obj: dict, resource: str) -> None:
        if "items" in obj:
            rows = [self._row(item) for item in obj["items"]]
            if not rows:
                print("No resources found" + ("" if self._namespace() else " in all namespaces") + ".")
                return
            headers = list(rows[0].keys())
            widths = {header: max(len(header), *(len(str(row.get(header, ""))) for row in rows)) for header in headers}
            print("   ".join(header.upper().ljust(widths[header]) for header in headers))
            for row in rows:
                print("   ".join(str(row.get(header, "")).ljust(widths[header]) for header in headers))
        else:
            for key, value in self._row(obj).items():
                print(f"{key.upper():14} {value}")

    def _row(self, obj: dict) -> dict[str, Any]:
        kind = obj.get("kind", "")
        meta = obj.get("metadata", {})
        status = obj.get("status", {})
        spec = obj.get("spec", {})
        row: dict[str, Any] = {"name": meta.get("name", "")}
        if self.all_namespaces or (self._namespace() is None):
            row = {"namespace": meta.get("namespace", ""), "name": meta.get("name", "")}
        if kind == "Pod":
            counts = status.get("containerStatuses", [])
            ready = sum(1 for c in counts if c.get("ready"))
            row["ready"] = f"{ready}/{len(spec.get('containers', []))}"
            row["status"] = status.get("phase", "Pending")
            row["restarts"] = sum(int(c.get("restartCount", 0)) for c in counts)
            row["age"] = _age(meta.get("creationTimestamp"))
            if self.output == "wide":
                row["ip"] = status.get("podIP", "")
                row["node"] = spec.get("nodeName", "")
        elif kind == "Deployment":
            desired = spec.get("replicas", 0)
            row["ready"] = f"{status.get('readyReplicas', 0)}/{desired}"
            row["up-to-date"] = status.get("updatedReplicas", 0)
            row["available"] = status.get("availableReplicas", 0)
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "ReplicaSet":
            row["desired"] = spec.get("replicas", 0)
            row["current"] = status.get("replicas", 0)
            row["ready"] = status.get("readyReplicas", 0)
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "StatefulSet":
            row["ready"] = f"{status.get('readyReplicas', 0)}/{spec.get('replicas', 0)}"
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "DaemonSet":
            row["desired"] = status.get("desiredNumberScheduled", 0)
            row["current"] = status.get("currentNumberScheduled", 0)
            row["ready"] = status.get("numberReady", 0)
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Job":
            row["completions"] = f"{status.get('succeeded', 0)}/{spec.get('completions', 1)}"
            row["duration"] = _duration(status.get("startTime"), status.get("completionTime"))
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "CronJob":
            row["schedule"] = spec.get("schedule", "")
            row["suspend"] = str(spec.get("suspend", False)).lower()
            row["active"] = len(status.get("active", []))
            row["last schedule"] = status.get("lastScheduleTime", "")
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Service":
            row["type"] = spec.get("type", "")
            row["cluster-ip"] = spec.get("clusterIP", "")
            ports = spec.get("ports", [])
            row["ports"] = ",".join(str(p.get("port")) + (f":{p.get('nodePort')}" if p.get("nodePort") else "") for p in ports)
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Endpoints":
            row["endpoints"] = ",".join(addr.get("ip", "") for subset in obj.get("subsets", []) for addr in subset.get("addresses", [])) or "<none>"
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "EndpointSlice":
            row["address-type"] = obj.get("addressType", "")
            row["endpoints"] = len(obj.get("endpoints", []))
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Node":
            conditions = {c.get("type"): c.get("status") for c in status.get("conditions", [])}
            row["status"] = "Ready" if conditions.get("Ready") == "True" else "NotReady"
            row["roles"] = "<none>"
            row["age"] = _age(meta.get("creationTimestamp"))
            if self.output == "wide":
                row["version"] = status.get("nodeInfo", {}).get("kubeletVersion", "")
        elif kind == "Namespace":
            row["status"] = "Terminating" if meta.get("deletionTimestamp") else "Active"
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind in ("ConfigMap", "Secret"):
            row["data"] = len(obj.get("data", {})) + len(obj.get("binaryData", {}))
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "PersistentVolumeClaim":
            row["status"] = status.get("phase", "Pending")
            row["volume"] = spec.get("volumeName", "")
            row["capacity"] = status.get("capacity", {}).get("storage", "")
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "PersistentVolume":
            row["capacity"] = spec.get("capacity", {}).get("storage", "")
            row["access modes"] = ",".join(spec.get("accessModes", []))
            row["reclaim policy"] = spec.get("persistentVolumeReclaimPolicy", "")
            row["status"] = status.get("phase", "")
            row["claim"] = f"{spec.get('claimRef', {}).get('namespace', '')}/{spec.get('claimRef', {}).get('name', '')}".strip("/")
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Ingress":
            rules = spec.get("rules", [])
            row["class"] = spec.get("ingressClassName", "")
            row["hosts"] = ",".join(rule.get("host", "*") for rule in rules) or "*"
            row["address"] = ",".join(i.get("ip", "") for i in status.get("loadBalancer", {}).get("ingress", []))
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "GatewayClass":
            row["controller"] = spec.get("controllerName", "")
            row["accepted"] = _condition_status(status, "Accepted")
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Gateway":
            row["class"] = spec.get("gatewayClassName", "")
            row["address"] = ",".join(item.get("value", "") for item in status.get("addresses", []))
            row["programmed"] = _condition_status(status, "Programmed")
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "HTTPRoute":
            row["hostnames"] = ",".join(spec.get("hostnames") or ["*"])
            parents = status.get("parents", [])
            row["parents"] = len(parents)
            row["accepted"] = ",".join(
                _condition_status(parent, "Accepted") for parent in parents) or "False"
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "ReferenceGrant":
            row["from"] = ",".join(
                f"{item.get('namespace')}/{item.get('kind', 'HTTPRoute')}"
                for item in spec.get("from", []))
            row["to"] = ",".join(
                f"{item.get('name', '*')}/{item.get('kind', 'Service')}"
                for item in spec.get("to", []))
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "HorizontalPodAutoscaler":
            row["reference"] = f"{spec.get('scaleTargetRef', {}).get('kind', '')}/{spec.get('scaleTargetRef', {}).get('name', '')}"
            row["targets"] = "cpu"
            row["minpods"] = spec.get("minReplicas", 1)
            row["maxpods"] = spec.get("maxReplicas", 1)
            row["replicas"] = status.get("currentReplicas", 0)
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "PodDisruptionBudget":
            row["min available"] = spec.get("minAvailable", "")
            row["max unavailable"] = spec.get("maxUnavailable", "")
            row["allowed disruptions"] = status.get("disruptionsAllowed", 0)
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "Event":
            row["type"] = obj.get("type", "")
            row["reason"] = obj.get("reason", "")
            row["object"] = f"{obj.get('involvedObject', {}).get('kind', '')}/{obj.get('involvedObject', {}).get('name', '')}"
            row["message"] = obj.get("message", "")
        elif kind == "CustomResourceDefinition":
            row["group"] = spec.get("group", "")
            row["scope"] = spec.get("scope", "")
            row["kind"] = spec.get("names", {}).get("kind", "")
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind == "ServiceAccount":
            row["secrets"] = len(obj.get("secrets", []))
            row["age"] = _age(meta.get("creationTimestamp"))
        elif kind in ("Role", "ClusterRole"):
            row["rules"] = len(obj.get("rules", []))
            row["age"] = _age(meta.get("creationTimestamp"))
        else:
            row["age"] = _age(meta.get("creationTimestamp"))
        return row

    def _replace_with_retry(self, api_version: str, kind: str, name: str,
                            namespace: str | None, mutator) -> dict:
        last_error = None
        for _ in range(6):
            obj = self.client.get(api_version, kind, name, namespace)
            mutated = mutator(obj)
            if mutated is None:
                return obj
            try:
                return self.client.replace(mutated)
            except ApiError as exc:
                if exc.status != 409:
                    raise
                last_error = exc
                time.sleep(0.1)
        raise last_error or ApiError(409, "Conflict", "unable to update object")

    # ---------------------------------------------------------------- commands
    def cmd_apply(self) -> int:
        paths = [self.args.filename]
        results = []
        options = {
            "server_side": getattr(self.args, "server_side", False),
            "field_manager": getattr(self.args, "field_manager", "mk8s-client"),
            "force": getattr(self.args, "force_conflicts", False),
        }
        for path in paths:
            if path == "-":
                text = sys.stdin.read()
                for document in yaml.safe_load_all(text):
                    if document:
                        result = self.client.apply(document, **options)
                        results.append(result)
                        if result.get("kind") == "CustomResourceDefinition":
                            self.client.invalidate_discovery()
            elif Path(path).is_dir():
                for child in sorted(Path(path).glob("*.y*ml")):
                    results.extend(self.client.apply_file(child, **options))
            else:
                results.extend(self.client.apply_file(path, **options))
        for result in results:
            verb = "created" if result["metadata"].get("creationTimestamp") else "configured"
            print(f'{result.get("kind", "").lower()}/{result["metadata"]["name"]} {verb}')
        return 0

    def cmd_create(self) -> int:
        return self.cmd_apply()

    def cmd_get(self) -> int:
        token = self.args.resource
        if token in ("all", "all.all"):
            for resource in ALL_RESOURCES:
                try:
                    api_version, info = self.resolve(resource)
                except ApiError:
                    continue
                result = self.client.list(api_version, info["kind"], self._namespace(), label_selector=self.args.selector)
                if result.get("items"):
                    self._print(result, resource)
                    print()
            return 0
        api_version, info = self.resolve(token)
        if self.args.names:
            objects = [self.client.get(api_version, info["kind"], name, self._namespace() if info.get("namespaced") else None)
                       for name in self.args.names]
            payload = objects[0] if len(objects) == 1 else {
                "apiVersion": "v1", "kind": info["kind"] + "List", "items": objects,
            }
        else:
            payload = self.client.list(api_version, info["kind"], self._namespace(), label_selector=self.args.selector)
        self._print(payload, resource=token)
        return 0

    def cmd_describe(self) -> int:
        api_version, info = self.resolve(self.args.resource)
        obj = self.client.get(api_version, info["kind"], self.args.name,
                              self._namespace() if info.get("namespaced") else None)
        kind = obj.get("kind", "")
        meta = obj.get("metadata", {})
        print(f"Name:         {meta.get('name')}")
        if meta.get("namespace"):
            print(f"Namespace:    {meta.get('namespace')}")
        print(f"Labels:       {','.join(f'{k}={v}' for k, v in meta.get('labels', {}).items()) or '<none>'}")
        print(f"Annotations:  {','.join(f'{k}={v}' for k, v in meta.get('annotations', {}).items()) or '<none>'}")
        print(f"API Version:  {obj.get('apiVersion')}")
        print(f"Kind:         {kind}")
        print(f"Metadata:\n  Creation Timestamp:  {meta.get('creationTimestamp')}\n  Resource Version:    {meta.get('resourceVersion')}\n  UID:                 {meta.get('uid')}")
        spec = obj.get("spec", {})
        if kind in ("Pod",):
            print(f"Node:         {spec.get('nodeName', '<none>')}")
            print(f"Status:       {obj.get('status', {}).get('phase')}")
            print(f"IP:           {obj.get('status', {}).get('podIP')}")
            print("Containers:")
            for container in spec.get("containers", []):
                print(f"  {container.get('name')}:")
                print(f"    Image:          {container.get('image')}")
                print(f"    Port:           {','.join(str(p.get('containerPort')) for p in container.get('ports', [])) or '<none>'}")
                requests = container.get("resources", {}).get("requests", {})
                limits = container.get("resources", {}).get("limits", {})
                print(f"    Requests:       {requests or '<none>'}")
                print(f"    Limits:         {limits or '<none>'}")
                print(f"    State:          {json.dumps(next(iter((obj.get('status', {}).get('containerStatuses') or [{}])[0].get('state', {}).values()), {}))}")
            print("Conditions:")
            for condition in obj.get("status", {}).get("conditions", []):
                print(f"  {condition.get('type'):18} {condition.get('status'):6} {condition.get('reason', '')}")
        elif kind in ("Deployment", "ReplicaSet", "StatefulSet", "DaemonSet"):
            print("Selector:     " + ",".join(f"{k}={v}" for k, v in spec.get("selector", {}).get("matchLabels", {}).items()))
            print(f"Replicas:     {spec.get('replicas', 1)} desired | {obj.get('status', {}).get('updatedReplicas', 0)} updated | "
                  f"{obj.get('status', {}).get('readyReplicas', 0)} ready | {obj.get('status', {}).get('availableReplicas', 0)} available")
            print("Pod Template:")
            for container in spec.get("template", {}).get("spec", {}).get("containers", []):
                print(f"  {container.get('name')}  {container.get('image')}")
            print("Conditions:")
            for condition in obj.get("status", {}).get("conditions", []):
                print(f"  {condition.get('type'):18} {condition.get('status'):6} {condition.get('reason', '')}")
        elif kind == "Service":
            print(f"Type:         {spec.get('type')}")
            print(f"Cluster IP:   {spec.get('clusterIP')}")
            print("Port:         " + "  ".join(f"{p.get('port')}/{p.get('protocol')} -> {p.get('targetPort')}" for p in spec.get("ports", [])))
            print(f"Selector:     " + ",".join(f"{k}={v}" for k, v in spec.get("selector", {}).items()))
        elif kind == "Node":
            status = obj.get("status", {})
            print(f"Taints:       {spec.get('taints', []) or '<none>'}")
            print(f"Unschedulable:{spec.get('unschedulable', False)}")
            print("Capacity:     " + ", ".join(f"{k}={v}" for k, v in status.get("capacity", {}).items()))
            print("Conditions:")
            for condition in status.get("conditions", []):
                print(f"  {condition.get('type'):18} {condition.get('status'):6} {condition.get('reason', '')}")
        else:
            print("Spec:")
            print(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).rstrip())
            if obj.get("status"):
                print("Status:")
                print(yaml.safe_dump(obj["status"], sort_keys=False, allow_unicode=True).rstrip())
        events = self.client.list("v1", "Event", meta.get("namespace"), field_selector=f"involvedObject.name={meta.get('name')}")
        if events.get("items"):
            print("Events:")
            for event in events["items"][-10:]:
                print(f"  {event.get('type'):8} {event.get('reason'):20} {event.get('message')}")
        return 0

    def cmd_delete(self) -> int:
        if self.args.filename:
            text = Path(self.args.filename).read_text(encoding="utf-8")
            for document in yaml.safe_load_all(text):
                if not document:
                    continue
                api_version, info = self.resolve(document["kind"])
                self.client.delete(api_version, info["kind"], document["metadata"]["name"],
                                   document.get("metadata", {}).get("namespace", self.namespace))
                print(f'{document["kind"].lower()}/{document["metadata"]["name"]} deleted')
            return 0
        if not self.args.resource:
            print("resource or -f is required", file=sys.stderr)
            return 2
        api_version, info = self.resolve(self.args.resource)
        names = self.args.names
        if self.args.all:
            listing = self.client.list(api_version, info["kind"], self._namespace())
            names = [item["metadata"]["name"] for item in listing["items"]]
        for name in names:
            self.client.delete(api_version, info["kind"], name, self._namespace() if info.get("namespaced") else None)
            print(f'{info["kind"].lower()}/{name} deleted')
        return 0

    def cmd_logs(self) -> int:
        if self.args.follow:
            print("follow is not supported by the simulated runtime; showing the current buffer", file=sys.stderr)
        sys.stdout.write(self.client.logs(self.args.pod, self.namespace, container=self.args.container,
                                          tail=self.args.tail, previous=self.args.previous))
        return 0

    def cmd_exec(self) -> int:
        command = self.args.command_args
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("a command is required after --", file=sys.stderr)
            return 2
        result = self.client.exec(self.args.pod, command, self.namespace, container=self.args.container)
        sys.stdout.write(result.get("stdout", ""))
        sys.stderr.write(result.get("stderr", ""))
        return 0 if result.get("status") == "Success" else 1

    def cmd_set(self) -> int:
        kind_token, _, name = self.args.target.partition("/")
        if not name:
            print("error: target must be resource/name", file=sys.stderr)
            return 2
        container_name, _, image = self.args.assignment.partition("=")
        if not image:
            print("error: assignment must be container=image", file=sys.stderr)
            return 2
        api_version, info = self.resolve(kind_token)
        obj = self.client.get(api_version, info["kind"], name, self.namespace)
        containers = obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        container = next((c for c in containers if c.get("name") == container_name), None)
        if container is None:
            print(f"error: container {container_name!r} not found", file=sys.stderr)
            return 1
        def mutate(current):
            containers = current.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            target = next((c for c in containers if c.get("name") == container_name), None)
            if target is None:
                return None
            target["image"] = image
            return current

        self._replace_with_retry(api_version, info["kind"], name, self.namespace, mutate)
        print(f'{info["kind"].lower()}/{name} image updated')
        return 0

    def cmd_scale(self) -> int:
        api_version, info = self.resolve(self.args.resource)
        result = self.client.scale(api_version, info["kind"], self.args.name, self.args.replicas,
                                   self.namespace)
        print(f'{info["kind"].lower()}/{self.args.name} scaled')
        return 0

    def cmd_rollout(self) -> int:
        api_version, info = self.resolve(self.args.resource)
        deployment = self.client.get(api_version, info["kind"], self.args.name, self.namespace)
        if self.args.action == "status":
            deadline = time.time() + self.args.timeout
            while time.time() < deadline:
                deployment = self.client.get(api_version, info["kind"], self.args.name, self.namespace)
                desired = deployment.get("spec", {}).get("replicas", 1)
                status = deployment.get("status", {})
                if status.get("updatedReplicas", 0) == desired and status.get("availableReplicas", 0) == desired:
                    print(f'deployment "{self.args.name}" successfully rolled out')
                    return 0
                time.sleep(0.5)
            print(f'error: deployment "{self.args.name}" exceeded its progress deadline', file=sys.stderr)
            return 1
        if self.args.action == "history":
            replicasets = self.client.list("apps/v1", "ReplicaSet", self.namespace).get("items", [])
            owned = [rs for rs in replicasets if any(ref.get("uid") == deployment["metadata"]["uid"]
                                                      for ref in rs["metadata"].get("ownerReferences", []))]
            for rs in sorted(owned, key=lambda r: int(r["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0"))):
                images = [c.get("image") for c in rs.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])]
                print(f'{rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "?")}  '
                      f'{rs["metadata"]["name"]}  {",".join(images)}')
            return 0
        if self.args.action == "undo":
            replicasets = self.client.list("apps/v1", "ReplicaSet", self.namespace).get("items", [])
            owned = [rs for rs in replicasets if any(ref.get("uid") == deployment["metadata"]["uid"]
                                                      for ref in rs["metadata"].get("ownerReferences", []))]
            current = int(deployment["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "1"))
            target_revision = self.args.to_revision or (current - 1)
            target = next((rs for rs in owned if int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0")) == target_revision), None)
            if target is None:
                print(f"error: unable to find revision {target_revision}", file=sys.stderr)
                return 1
            template = json.loads(json.dumps(target["spec"]["template"]))
            template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
            template.get("metadata", {}).get("annotations", {}).pop("pod-template-hash", None)
            def mutate(current):
                current["spec"]["template"] = template
                return current

            self._replace_with_retry(api_version, info["kind"], self.args.name, self.namespace, mutate)
            print(f'deployment.apps/{self.args.name} rolled back')
            return 0
        if self.args.action == "restart":
            def mutate(current):
                template = current.setdefault("spec", {}).setdefault("template", {})
                template.setdefault("metadata", {}).setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = datetime.now(timezone.utc).isoformat()
                return current

            self._replace_with_retry(api_version, info["kind"], self.args.name, self.namespace, mutate)
            print(f'deployment.apps/{self.args.name} restarted')
            return 0
        return 2

    def cmd_expose(self) -> int:
        deployment = self.client.get("apps/v1", "Deployment", self.args.deployment, self.namespace)
        name = self.args.name or self.args.deployment
        labels = deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
        body = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {"selector": labels, "type": self.args.type,
                     "ports": [{"port": self.args.port,
                                "targetPort": self.args.target_port or self.args.port}]},
        }
        self.client.create(body)
        print(f"service/{name} exposed")
        return 0

    def cmd_run(self) -> int:
        command = self.args.command_args or []
        if command and command[0] == "--":
            command = command[1:]
        env = []
        for item in self.args.env:
            key, _, value = item.partition("=")
            env.append({"name": key, "value": value})
        container = {"name": self.args.name, "image": self.args.image, "env": env}
        if self.args.port:
            container["ports"] = [{"containerPort": self.args.port}]
        if command:
            container["command"] = command
        if self.args.replicas > 1:
            body = {
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": self.args.name, "namespace": self.namespace},
                "spec": {"replicas": self.args.replicas,
                         "selector": {"matchLabels": {"run": self.args.name}},
                         "template": {"metadata": {"labels": {"run": self.args.name}},
                                      "spec": {"containers": [container]}}},
            }
        else:
            body = {
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": self.args.name, "namespace": self.namespace, "labels": {"run": self.args.name}},
                "spec": {"containers": [container]},
            }
        self.client.create(body)
        print(f'{body["kind"].lower()}/{self.args.name} created')
        return 0

    def cmd_cordon(self) -> int:
        def mutate(node):
            node.setdefault("spec", {})["unschedulable"] = True
            return node

        self._replace_with_retry("v1", "Node", self.args.node, None, mutate)
        print(f"node/{self.args.node} cordoned")
        return 0

    def cmd_uncordon(self) -> int:
        def mutate(node):
            node.setdefault("spec", {})["unschedulable"] = False
            return node

        self._replace_with_retry("v1", "Node", self.args.node, None, mutate)
        print(f"node/{self.args.node} uncordoned")
        return 0

    def cmd_drain(self) -> int:
        self.cmd_cordon()
        pods = self.client.list("v1", "Pod", None, field_selector=f"spec.nodeName={self.args.node}")["items"]
        for pod in pods:
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            owners = [ref for ref in pod["metadata"].get("ownerReferences", []) if ref.get("kind") == "DaemonSet"]
            if owners:
                if not self.args.ignore_daemonsets:
                    print(f"error: cannot drain node with DaemonSet pod {pod['metadata']['name']}; use --ignore-daemonsets", file=sys.stderr)
                    return 1
                continue
            namespace = pod["metadata"]["namespace"]
            try:
                self.client.evict(pod["metadata"]["name"], namespace)
                print(f'pod/{pod["metadata"]["name"]} evicted')
            except ApiError as exc:
                if not self.args.force:
                    print(f"error: {exc.message}", file=sys.stderr)
                    return 1
                self.client.delete("v1", "Pod", pod["metadata"]["name"], namespace)
        print(f"node/{self.args.node} drained")
        return 0

    def cmd_taint(self) -> int:
        spec = self.args.taint
        remove = spec.endswith("-")
        key = spec[:-1] if remove else spec
        value = ""
        effect = ""
        if not remove:
            body, _, effect = spec.replace("=", ":", 1).rpartition(":") if ":" in spec else (spec, "", "")
            key, _, value = body.partition(":")
            if not effect:
                print("error: taint must be key[=value]:effect", file=sys.stderr)
                return 2

        def mutate(node):
            taints = list(node.get("spec", {}).get("taints", []))
            if remove:
                taints = [t for t in taints if t.get("key") != key]
            else:
                existing = next((t for t in taints if t.get("key") == key and t.get("effect") == effect), None)
                if existing and not self.args.overwrite:
                    raise ApiError(409, "Conflict", "taint already exists; use --overwrite")
                taints = [t for t in taints if not (t.get("key") == key and t.get("effect") == effect)]
                taints.append({"key": key, "value": value, "effect": effect})
            node.setdefault("spec", {})["taints"] = taints
            return node

        try:
            self._replace_with_retry("v1", "Node", self.args.node, None, mutate)
        except ApiError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 1
        print(f"node/{self.args.node} tainted")
        return 0

    def cmd_auth(self) -> int:
        group = ""
        resource = self.args.resource
        if "." in resource:
            resource, group = resource.split(".", 1)
        allowed, reason = self.client.can_i(self.args.verb, resource, namespace=self.namespace, group=group)
        print("yes" if allowed else "no")
        if not allowed:
            print(reason, file=sys.stderr)
        return 0 if allowed else 1

    def cmd_api_resources(self) -> int:
        rows = []
        for (group, version), resources in self.client.discover().items():
            if self.args.api_group and group != self.args.api_group:
                continue
            for kind, info in resources.items():
                rows.append({
                    "name": info["name"], "shortnames": ",".join(info.get("shortNames", [])),
                    "apiversion": f"{group}/{version}" if group else version,
                    "namespaced": info.get("namespaced"), "kind": kind,
                })
        print("NAME".ljust(40) + "SHORTNAMES".ljust(15) + "APIVERSION".ljust(40) + "NAMESPACED".ljust(12) + "KIND")
        for row in sorted(rows, key=lambda r: r["name"]):
            print(row["name"].ljust(40) + row["shortnames"].ljust(15) + row["apiversion"].ljust(40) +
                  str(row["namespaced"]).lower().ljust(12) + row["kind"])
        return 0

    def cmd_explain(self) -> int:
        api_version, info = self.resolve(self.args.resource)
        print(f"KIND:     {info['kind']}")
        print(f"VERSION:  {api_version}\n")
        print("DESCRIPTION:")
        print(f"     {info['kind']} is an API object served by MiniK8s.\n")
        fields = {
            "Pod": ["metadata.name", "spec.containers[]", "spec.initContainers[]", "spec.volumes[]",
                    "spec.nodeName", "spec.restartPolicy", "status.phase", "status.conditions[]"],
            "Deployment": ["spec.replicas", "spec.selector", "spec.template", "spec.strategy",
                           "status.availableReplicas", "status.updatedReplicas"],
            "Service": ["spec.selector", "spec.ports[]", "spec.type", "spec.clusterIP"],
            "Node": ["spec.unschedulable", "spec.taints[]", "status.capacity", "status.conditions[]"],
        }
        print("FIELDS:")
        for field in fields.get(info["kind"], ["metadata.name", "metadata.labels", "metadata.annotations", "spec", "status"]):
            print(f"   {field}")
        return 0

    def cmd_top(self) -> int:
        if self.args.what == "pods":
            result = self.client.list("metrics.k8s.io/v1beta1", "PodMetrics", self._namespace())
            print("NAMESPACE".ljust(20) + "NAME".ljust(40) + "CPU(cores)".ljust(15) + "MEMORY(bytes)")
            for item in result.get("items", []):
                cpu = 0.0
                memory = 0.0
                for container in item.get("containers", []):
                    cpu += float(str(container.get("usage", {}).get("cpu", "0")).rstrip("m") or 0)
                    memory += float(container.get("usage", {}).get("memory", 0))
                print(item["metadata"].get("namespace", "").ljust(20) + item["metadata"]["name"].ljust(40) +
                      f"{int(cpu)}m".ljust(15) + f"{int(memory / 1024 / 1024)}Mi")
        else:
            result = self.client.list("metrics.k8s.io/v1beta1", "NodeMetrics")
            print("NAME".ljust(40) + "CPU(cores)".ljust(15) + "MEMORY(bytes)")
            for item in result.get("items", []):
                print(item["metadata"]["name"].ljust(40) +
                      str(item.get("usage", {}).get("cpu", "")).ljust(15) +
                      f'{int(float(item.get("usage", {}).get("memory", 0)) / 1024 / 1024)}Mi')
        return 0

    def cmd_cluster_info(self) -> int:
        version = self.client.request("GET", "/version")
        print("Kubernetes control plane is running at " + self.args.server)
        print(f"MiniK8s version: {version.get('gitVersion')}")
        return 0

    def cmd_version(self) -> int:
        version = self.client.request("GET", "/version")
        print(f"Client Version: v1.31.0-minik8s")
        print(f"Server Version: {version.get('gitVersion')}")
        return 0

    def cmd_wait(self) -> int:
        condition = self.args.condition
        if "=" not in condition:
            print("error: --for must be condition=value", file=sys.stderr)
            return 2
        _, _, wanted = condition.partition("=")
        api_version, info = self.resolve(self.args.resource)
        deadline = time.time() + self.args.timeout
        while time.time() < deadline:
            obj = self.client.get(api_version, info["kind"], self.args.name, self.namespace)
            conditions = obj.get("status", {}).get("conditions", [])
            if any(c.get("type") == wanted and c.get("status") == "True" for c in conditions):
                print(f'{info["kind"].lower()}/{self.args.name} condition met')
                return 0
            time.sleep(0.5)
        print(f"timed out waiting for {condition}", file=sys.stderr)
        return 1


def _condition_status(obj: dict, condition_type: str) -> str:
    for item in obj.get("conditions", []) or []:
        if item.get("type") == condition_type:
            return item.get("status", "Unknown")
    return "Unknown"


def _age(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    try:
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _duration(start: str | None, end: str | None) -> str:
    if not start:
        return ""
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(timezone.utc)
    except ValueError:
        return ""
    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    return f"{seconds}s"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return CLI(args).run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
