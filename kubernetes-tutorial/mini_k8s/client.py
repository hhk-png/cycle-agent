"""A small Kubernetes client used by the CLI and integration tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

import yaml

from mini_k8s.errors import ApiError


class ApiClient:
    def __init__(self, server: str = "http://127.0.0.1:8080", *, token: str | None = None,
                 timeout: float = 10.0):
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._discovery: dict[tuple[str, str], dict[str, dict]] | None = None

    # ------------------------------------------------------------------ HTTP
    def request(self, method: str, path: str, body: Any = None, *,
                content_type: str = "application/json", params: dict[str, Any] | None = None,
                raw: bool = False, timeout: float | None = None) -> Any:
        url = self.server + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            if query:
                url += ("&" if "?" in url else "?") + query
        headers = {"Accept": "application/json", "Connection": "close"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            if isinstance(body, (bytes, str)):
                data = body.encode() if isinstance(body, str) else body
            elif content_type in ("application/yaml", "application/apply-patch+yaml"):
                data = yaml.safe_dump(body).encode()
            else:
                data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
                if raw:
                    return payload.decode("utf-8")
                if not payload:
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload)
                raise ApiError(parsed.get("code", exc.code), parsed.get("reason", "HTTPError"),
                               parsed.get("message", str(exc)), parsed.get("details")) from None
            except json.JSONDecodeError:
                raise ApiError(exc.code, "HTTPError", payload.decode("utf-8", "replace")) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "ConnectionError", f"cannot connect to {self.server}: {exc.reason}") from None

    # ---------------------------------------------------------------- discovery
    def invalidate_discovery(self) -> None:
        self._discovery = None

    def discover(self) -> dict[tuple[str, str], dict[str, dict]]:
        if self._discovery is not None:
            return self._discovery
        result: dict[tuple[str, str], dict[str, dict]] = {}
        core = self.request("GET", "/api/v1")
        result[("", "v1")] = {r["kind"]: r for r in core.get("resources", [])}
        groups = self.request("GET", "/apis").get("groups", [])
        for group in groups:
            for version in group.get("versions", []):
                data = self.request("GET", f"/apis/{version['groupVersion']}")
                result[(group["name"], version["version"])] = {r["kind"]: r for r in data.get("resources", [])}
        self._discovery = result
        return result

    def resource_info(self, api_version: str, kind: str) -> dict:
        if "/" in api_version:
            group, version = api_version.split("/", 1)
        else:
            group, version = "", api_version
        resources = self.discover().get((group, version), {})
        if kind not in resources:
            raise ApiError(400, "BadRequest", f"no resource {kind} in {api_version}")
        return resources[kind]

    def path_for(self, api_version: str, kind: str, namespace: str | None = None, name: str | None = None,
                 subresource: str | None = None) -> str:
        info = self.resource_info(api_version, kind)
        if "/" in api_version:
            group, version = api_version.split("/", 1)
            prefix = f"/apis/{group}/{version}"
        else:
            version = api_version
            prefix = f"/api/{version}"
        plural = info["name"]
        if info.get("namespaced") and namespace is not None:
            prefix += f"/namespaces/{urllib.parse.quote(namespace)}"
        prefix += f"/{plural}"
        if name:
            prefix += f"/{urllib.parse.quote(name)}"
        if subresource:
            prefix += f"/{subresource}"
        return prefix

    # ---------------------------------------------------------------- CRUD
    def get(self, api_version: str, kind: str, name: str, namespace: str | None = None) -> dict:
        info = self.resource_info(api_version, kind)
        if info.get("namespaced") and namespace is None:
            namespace = "default"
        return self.request("GET", self.path_for(api_version, kind, namespace, name))

    def list(self, api_version: str, kind: str, namespace: str | None = None, *,
             label_selector: str | None = None, field_selector: str | None = None,
             limit: int | None = None, continue_token: str | None = None) -> dict:
        params = {"labelSelector": label_selector, "fieldSelector": field_selector,
                  "limit": limit, "continue": continue_token}
        return self.request("GET", self.path_for(api_version, kind, namespace), params=params)

    def create(self, obj: dict) -> dict:
        info = self.resource_info(obj["apiVersion"], obj["kind"])
        namespace = obj.get("metadata", {}).get("namespace", "default") if info.get("namespaced") else None
        return self.request("POST", self.path_for(obj["apiVersion"], obj["kind"], namespace), obj)

    def replace(self, obj: dict) -> dict:
        info = self.resource_info(obj["apiVersion"], obj["kind"])
        namespace = obj.get("metadata", {}).get("namespace", "default") if info.get("namespaced") else None
        return self.request("PUT", self.path_for(obj["apiVersion"], obj["kind"], namespace,
                                                 obj["metadata"]["name"]), obj)

    def delete(self, api_version: str, kind: str, name: str, namespace: str | None = None,
               *, propagation: str | None = None) -> dict:
        body = {"apiVersion": "v1", "kind": "DeleteOptions"}
        if propagation:
            body["propagationPolicy"] = propagation
        info = self.resource_info(api_version, kind)
        if info.get("namespaced") and namespace is None:
            namespace = "default"
        return self.request("DELETE", self.path_for(api_version, kind, namespace, name), body)

    def patch(self, api_version: str, kind: str, name: str, patch: Any, namespace: str | None = None, *,
              content_type: str = "application/merge-patch+json",
              params: dict[str, Any] | None = None) -> dict:
        info = self.resource_info(api_version, kind)
        if info.get("namespaced") and namespace is None:
            namespace = "default"
        return self.request("PATCH", self.path_for(api_version, kind, namespace, name), patch,
                            content_type=content_type, params=params)

    def apply(self, obj: dict, *, server_side: bool = False, field_manager: str = "mk8s-client",
              force: bool = False) -> dict:
        info = self.resource_info(obj["apiVersion"], obj["kind"])
        namespace = obj.get("metadata", {}).get("namespace", "default") if info.get("namespaced") else None
        name = obj["metadata"]["name"]
        if server_side:
            result = self.patch(
                obj["apiVersion"], obj["kind"], name, obj, namespace,
                content_type="application/apply-patch+yaml",
                params={"fieldManager": field_manager, "force": str(force).lower()},
            )
            if obj.get("kind") == "CustomResourceDefinition":
                self.invalidate_discovery()
            return result
        existing = None
        try:
            existing = self.get(obj["apiVersion"], obj["kind"], name, namespace)
        except ApiError as exc:
            if exc.status != 404:
                raise
        if existing is None:
            result = self.create(obj)
        else:
            merged = _deep_merge(existing, obj)
            merged["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            result = self.replace(merged)
        if obj.get("kind") == "CustomResourceDefinition":
            self.invalidate_discovery()
        return result

    def apply_file(self, path: str | Any, *, server_side: bool = False,
                   field_manager: str = "mk8s-client", force: bool = False) -> list[dict]:
        from pathlib import Path
        text = Path(path).read_text(encoding="utf-8") if not hasattr(path, "read") else path.read()
        results = []
        options = {"server_side": server_side, "field_manager": field_manager, "force": force}
        for document in yaml.safe_load_all(text):
            if document is None:
                continue
            items = document["items"] if document.get("kind", "").endswith("List") and "items" in document else [document]
            for item in items:
                if item.get("kind") == "CustomResourceDefinition":
                    results.append(self.apply(item, **options))
                    self.invalidate_discovery()
                    continue
                try:
                    results.append(self.apply(item, **options))
                except ApiError as exc:
                    if exc.status == 400 and "no resource" in exc.message:
                        self.invalidate_discovery()
                        results.append(self.apply(item, **options))
                    else:
                        raise
        return results

    # ---------------------------------------------------------------- subresources
    def logs(self, pod: str, namespace: str = "default", *, container: str | None = None,
             tail: int = 200, previous: bool = False) -> str:
        path = self.path_for("v1", "Pod", namespace, pod, "log")
        return self.request("GET", path, params={"container": container, "tailLines": tail,
                                                 "previous": str(previous).lower()}, raw=True)

    def exec(self, pod: str, command: list[str], namespace: str = "default", *,
             container: str | None = None) -> dict:
        path = self.path_for("v1", "Pod", namespace, pod, "exec")
        params = [("command", item) for item in command]
        if container:
            params.append(("container", container))
        query = urllib.parse.urlencode(params)
        return self.request("POST", path + "?" + query, {})

    def scale(self, api_version: str, kind: str, name: str, replicas: int | None,
              namespace: str = "default") -> dict:
        path = self.path_for(api_version, kind, namespace, name, "scale")
        if replicas is None:
            return self.request("GET", path)
        return self.request("PUT", path, {"apiVersion": "autoscaling/v1", "kind": "Scale",
                                          "metadata": {"name": name, "namespace": namespace},
                                          "spec": {"replicas": replicas}})

    def evict(self, pod: str, namespace: str = "default") -> dict:
        path = self.path_for("v1", "Pod", namespace, pod, "eviction")
        return self.request("POST", path, {"apiVersion": "policy/v1", "kind": "Eviction",
                                           "metadata": {"name": pod, "namespace": namespace}})

    def can_i(self, verb: str, resource: str, *, namespace: str | None = "default",
              group: str = "", name: str | None = None) -> tuple[bool, str]:
        path = self.path_for("authorization.k8s.io/v1", "SelfSubjectAccessReview")
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"verb": verb, "resource": resource,
                                                "namespace": namespace, "group": group, "name": name}}}
        result = self.request("POST", path, body)
        return bool(result.get("status", {}).get("allowed")), result.get("status", {}).get("reason", "")

    # --------------------------------------------------------------------- watch
    def watch(self, api_version: str, kind: str, namespace: str | None = None, *,
              resource_version: str | None = None, timeout_seconds: int = 10,
              label_selector: str | None = None, allow_bookmarks: bool = False) -> Iterator[dict]:
        path = self.path_for(api_version, kind, namespace)
        params = urllib.parse.urlencode({"watch": "true", "resourceVersion": resource_version or "",
                                         "timeoutSeconds": timeout_seconds,
                                         "labelSelector": label_selector or "",
                                         "allowWatchBookmarks": str(allow_bookmarks).lower()})
        url = self.server + path + "?" + params
        headers = {"Accept": "application/json", "Connection": "close"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout_seconds + 5) as response:
            for line in response:
                if line.strip():
                    yield json.loads(line)


def _deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _deep_merge(result.get(key), value)
    return result
