"""Client abstractions used by controllers, the CLI and the API server."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .. import constants as C
from ..errors import ApiError, BadRequest, Conflict, NotFound
from ..store.store import ApiStore

PLURALS = {kind: plural for plural, kind in C.PLURAL_TO_KIND.items()}


def api_prefix(kind: str) -> str:
    if kind in (C.KIND_POD, C.KIND_SERVICE, C.KIND_EVENT, C.KIND_SECRET, C.KIND_PVC, C.KIND_SERVICEACCOUNT):
        return "/api/v1"
    if kind == C.KIND_JOB:
        return "/apis/batch/v1"
    if kind == C.KIND_ENDPOINTSLICE:
        return "/apis/discovery.k8s.io/v1"
    if kind == C.KIND_NETWORKPOLICY or kind == C.KIND_INGRESS:
        return "/apis/networking.k8s.io/v1"
    if kind == C.KIND_HTTPROUTE or kind == C.KIND_GATEWAY:
        return "/apis/gateway.networking.k8s.io/v1"
    if kind == C.KIND_PODGROUP:
        return "/apis/scheduling.volcano.sh/v1beta1"
    return f"/apis/{C.RAY_API_VERSION}"


def resource_path(kind: str, namespace: str | None, name: str | None = None, subresource: str | None = None, *, all_namespaces: bool = False) -> str:
    plural = PLURALS.get(kind)
    if not plural:
        raise BadRequest(f"unknown kind: {kind}")
    if namespace is None or all_namespaces:
        path = f"{api_prefix(kind)}/{plural}"
    else:
        path = f"{api_prefix(kind)}/namespaces/{namespace}/{plural}"
    if name:
        path += f"/{name}"
    if subresource:
        path += f"/{subresource}"
    return path


class KubeClient(Protocol):
    def get(self, kind: str, name: str, namespace: str = "default") -> dict: ...
    def list(self, kind: str, namespace: str | None = None, label_selector: dict | None = None) -> list[dict]: ...
    def create(self, kind: str, obj: dict, namespace: str | None = None) -> dict: ...
    def update(self, kind: str, obj: dict, subresource: str | None = None) -> dict: ...
    def update_status(self, obj: dict) -> dict: ...
    def delete(self, kind: str, name: str, namespace: str = "default", force: bool = False) -> dict: ...


class LocalClient:
    """Direct store access; used by the operator and tests."""

    def __init__(self, store: ApiStore):
        self.store = store

    def get(self, kind: str, name: str, namespace: str = "default") -> dict:
        return self.store.get(kind, name, namespace)

    def list(self, kind: str, namespace: str | None = None, label_selector: dict | None = None) -> list[dict]:
        return self.store.list(kind, namespace, label_selector=label_selector)

    def create(self, kind: str, obj: dict, namespace: str | None = None) -> dict:
        return self.store.create(kind, obj, namespace)

    def update(self, kind: str, obj: dict, subresource: str | None = None) -> dict:
        return self.store.update(kind, obj, subresource=subresource)

    def update_status(self, obj: dict) -> dict:
        return self.store.update_status(obj)

    def delete(self, kind: str, name: str, namespace: str = "default", force: bool = False) -> dict:
        return self.store.delete(kind, name, namespace, force=force)


class HTTPClient:
    """Thin Kubernetes-REST-like client.

    The API server intentionally uses the same paths as the real Kubernetes
    API, so this client doubles as a teaching aid for ``kubectl`` debugging.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8080", token: str | None = None, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None, *, raw: bool = False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if raw:
                    return payload
                return json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                status = json.loads(payload.decode("utf-8"))
            except Exception:
                status = {"message": payload.decode("utf-8", "replace"), "code": exc.code}
            error_cls = {404: NotFound, 409: Conflict, 400: BadRequest}.get(exc.code, ApiError)
            raise error_cls(status.get("message", str(exc)), status=exc.code, reason=status.get("reason")) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"cannot reach {self.base_url}: {exc.reason}", status=503, reason="ServiceUnavailable") from exc

    def get(self, kind: str, name: str, namespace: str = "default") -> dict:
        return self._request("GET", resource_path(kind, namespace, name))

    def list(self, kind: str, namespace: str | None = None, label_selector: dict | None = None) -> list[dict]:
        path = resource_path(kind, namespace, all_namespaces=namespace is None)
        if label_selector:
            selector = ",".join(f"{key}={value}" for key, value in label_selector.items())
            path += "?" + urllib.parse.urlencode({"labelSelector": selector})
        result = self._request("GET", path)
        return result.get("items", result if isinstance(result, list) else [])

    def create(self, kind: str, obj: dict, namespace: str | None = None) -> dict:
        ns = namespace or (obj.get("metadata") or {}).get("namespace", "default")
        return self._request("POST", resource_path(kind, ns), obj)

    def update(self, kind: str, obj: dict, subresource: str | None = None) -> dict:
        ns = (obj.get("metadata") or {}).get("namespace", "default")
        name = (obj.get("metadata") or {}).get("name")
        return self._request("PUT", resource_path(kind, ns, name, subresource), obj)

    def update_status(self, obj: dict) -> dict:
        return self.update(obj["kind"], obj, subresource="status")

    def delete(self, kind: str, name: str, namespace: str = "default", force: bool = False) -> dict:
        path = resource_path(kind, namespace, name)
        if force:
            path += "?force=true"
        return self._request("DELETE", path)

    def raw_metrics(self) -> str:
        return self._request("GET", "/metrics", raw=True).decode("utf-8")

    def state(self, dataset: str, **params) -> dict:
        """Query the Ray-style State API subset (``/api/v0/<dataset>``)."""
        query = {key: value for key, value in params.items() if value not in (None, "")}
        path = f"/api/v0/{dataset.strip('/')}"
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request("GET", path)
