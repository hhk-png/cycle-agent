"""The MiniK8s API server: HTTP, discovery, authn/authz, admission, watch."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import yaml

from mini_k8s import __version__
from mini_k8s.admission.admission import AdmissionChain, AdmissionRequest
from mini_k8s.apimachinery.apply import APPLY_CONTENT_TYPE, apply_object
from mini_k8s.apimachinery.meta import deep_copy, json_patch, merge_patch, now_iso, parse_time
from mini_k8s.apimachinery.selectors import match_labels
from mini_k8s.auth.authn import ServiceAccountTokenIssuer, StaticTokenAuthenticator, User
from mini_k8s.auth.authz import RBACAuthorizer, RequestAttributes
from mini_k8s.errors import ApiError, bad_request, forbidden, internal, not_found, unauthorized
from mini_k8s.store.store import GVR, ResourceStore, WatchEvent


@dataclass
class Route:
    kind: str  # discovery | resource | health | version | openapi | metrics
    group: str = ""
    version: str = ""
    plural: str = ""
    namespace: str | None = None
    name: str | None = None
    subresource: str | None = None
    extra: list[str] | None = None
    path: str = ""
    group_only: bool = False


class APIServer:
    def __init__(
        self,
        store: ResourceStore,
        registry,
        admission: AdmissionChain,
        *,
        authenticator: StaticTokenAuthenticator | None = None,
        authorizer: RBACAuthorizer | None = None,
        token_issuer: ServiceAccountTokenIssuer | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        log_provider: Callable[..., str] | None = None,
        exec_provider: Callable[..., dict] | None = None,
        verbose: bool = False,
    ):
        self.store = store
        self.registry = registry
        self.admission = admission
        self.authenticator = authenticator or StaticTokenAuthenticator({
            "mini-admin-token": User("system:mini-admin", ["system:masters", "system:authenticated"])
        })
        self.authorizer = authorizer
        self.token_issuer = token_issuer
        self.host = host
        self.port = port
        self.log_provider = log_provider
        self.exec_provider = exec_provider
        self.verbose = verbose
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._crds: set[GVR] = set()
        self._api_resources: set[tuple[str, str, str]] = set()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> "APIServer":
        if self.httpd:
            return self
        server = self

        class BoundHandler(APIHandler):
            api_server = server

        self.httpd = ThreadingHTTPServer((self.host, self.port), BoundHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="mini-k8s-apiserver", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ------------------------------------------------------------ CRD discovery
    def sync_crds(self) -> None:
        crd_gvr = ("apiextensions.k8s.io", "v1", "customresourcedefinitions")
        desired: dict[GVR, Any] = {}
        for crd in self.store.list(crd_gvr)[0]:
            try:
                for definition in self.registry.from_crd(crd):
                    desired[definition.gvr] = definition
            except ApiError:
                continue
        for gvr in list(self._crds):
            if gvr not in desired and self.registry.maybe_get(*gvr):
                definition = self.registry.maybe_get(*gvr)
                if definition and definition.group not in ("", "apps", "batch", "autoscaling", "networking.k8s.io",
                                                          "storage.k8s.io", "rbac.authorization.k8s.io", "policy",
                                                          "scheduling.k8s.io", "apiextensions.k8s.io",
                                                          "coordination.k8s.io", "discovery.k8s.io",
                                                          "admissionregistration.k8s.io", "metrics.k8s.io",
                                                          "authorization.k8s.io", "authentication.k8s.io"):
                    self.registry.unregister(definition)
        for gvr, definition in desired.items():
            self.registry.register(definition)
        self._crds = set(desired)

    # ------------------------------------------------------------------ helpers
    def resource_list(self, group: str, version: str) -> dict:
        resources = []
        for definition in self.registry.for_group_version(group, version):
            resources.append({
                "name": definition.plural,
                "singularName": definition.singular,
                "namespaced": definition.namespaced,
                "kind": definition.kind,
                "verbs": list(definition.verbs),
                "shortNames": list(definition.short_names),
                "categories": list(definition.categories),
            })
        return {
            "kind": "APIResourceList",
            "apiVersion": "v1",
            "groupVersion": f"{group}/{version}" if group else version,
            "resources": resources,
        }

    def api_group(self, group: str) -> dict:
        versions = sorted(self.registry.versions().get(group, set()))
        version_entries = [{"groupVersion": f"{group}/{v}", "version": v} for v in versions]
        preferred = versions[0] if versions else "v1"
        return {
            "kind": "APIGroup",
            "apiVersion": "v1",
            "name": group,
            "versions": version_entries,
            "preferredVersion": {"groupVersion": f"{group}/{preferred}", "version": preferred},
        }


class APIHandler(BaseHTTPRequestHandler):
    server_version = f"MiniK8s/{__version__}"
    protocol_version = "HTTP/1.1"
    api_server: APIServer

    # ------------------------------------------------------------------- verbs
    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.api_server.verbose:
            print(f"[api] {self.address_string()} {fmt % args}")

    # ---------------------------------------------------------------- dispatch
    def _dispatch(self) -> None:
        server = self.api_server
        self._body_read = False
        try:
            server.sync_crds()
            user = server.authenticator.authenticate(self.headers.get("Authorization"))
            route = self._parse_route()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if route.kind == "health":
                self._send_json(200, {"status": "ok"})
                return
            if route.kind == "version":
                self._send_json(200, {
                    "major": "1", "minor": "31", "gitVersion": f"v1.31.0-minik8s.{__version__}",
                    "gitCommit": "mini", "gitTreeState": "clean", "buildDate": "2026-01-01T00:00:00Z",
                    "goVersion": "python", "compiler": "cpython", "platform": "mini/amd64",
                })
                return
            if route.kind == "openapi":
                self._send_json(200, {"swagger": "2.0", "info": {"title": "MiniK8s", "version": __version__}, "paths": {}})
                return
            if route.kind == "metrics":
                self._send_text(200, self._metrics())
                return
            if route.kind == "discovery":
                self._discovery(route)
                return
            self._authorize(route, user, query)
            self._handle_resource(route, user, query)
        except ApiError as exc:
            self._send_json(exc.status, exc.to_status())
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - defensive boundary
            if server.verbose:
                import traceback
                traceback.print_exc()
            err = internal(f"unexpected server error: {exc}")
            self._send_json(err.status, err.to_status())

    # ------------------------------------------------------------------ routing
    def _parse_route(self) -> Route:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/healthz", "/livez", "/readyz"):
            return Route("health", path=path)
        if path == "/version":
            return Route("version", path=path)
        if path.startswith("/openapi"):
            return Route("openapi", path=path)
        if path == "/metrics":
            return Route("metrics", path=path)
        if path == "/api":
            return Route("discovery", path=path, group_only=True)
        if path == "/apis":
            return Route("discovery", path=path, group_only=True)
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        if not parts:
            return Route("discovery", path=path, group_only=True)
        if parts[0] == "api":
            if len(parts) < 2:
                return Route("discovery", path=path, group_only=True)
            group, version, rest = "", parts[1], parts[2:]
        elif parts[0] == "apis":
            if len(parts) == 1:
                return Route("discovery", path=path, group_only=True)
            if len(parts) == 2:
                return Route("discovery", path=path, group=parts[1], group_only=True)
            group, version, rest = parts[1], parts[2], parts[3:]
        else:
            raise not_found("path", path)
        if not rest:
            return Route("discovery", path=path, group=group, version=version)
        namespace: str | None = None
        if rest[0] == "namespaces" and len(rest) >= 3 and rest[2] not in ("status", "finalize"):
            namespace = rest[1]
            rest = rest[2:]
        plural = rest[0]
        name = rest[1] if len(rest) > 1 else None
        subresource = rest[2] if len(rest) > 2 else None
        extra = rest[3:] if len(rest) > 3 else []
        return Route("resource", group=group, version=version, plural=plural, namespace=namespace,
                     name=name, subresource=subresource, extra=extra, path=path)

    def _discovery(self, route: Route) -> None:
        server = self.api_server
        if route.path == "/api":
            self._send_json(200, {
                "kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"],
                "serverAddressByClientCIDRs": [{"clientCIDR": "0.0.0.0/0", "serverAddress": server.url}],
            })
            return
        if route.path == "/apis":
            groups = []
            for group in sorted(server.registry.versions()):
                if group:
                    info = server.api_group(group)
                    groups.append({k: info[k] for k in ("name", "versions", "preferredVersion")})
            self._send_json(200, {
                "kind": "APIGroupList", "apiVersion": "v1", "groups": groups,
            })
            return
        if route.group and route.group_only:
            self._send_json(200, server.api_group(route.group))
            return
        if not route.group and not route.version:
            self._send_json(200, {"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"]})
            return
        self._send_json(200, server.resource_list(route.group, route.version))

    # ---------------------------------------------------------------- authorize
    def _authorize(self, route: Route, user: User, query: dict[str, list[str]]) -> None:
        server = self.api_server
        if server.authorizer is None:
            return
        method = self.command
        subresource = route.subresource
        if route.plural == "pods" and subresource in ("log", "exec", "portforward", "proxy"):
            verb = "get" if method == "GET" else "create"
        elif subresource in ("status", "scale", "binding", "eviction", "token"):
            verb = "get" if method == "GET" else ("create" if subresource in ("binding", "eviction", "token") else "update")
        elif method == "GET":
            verb = "watch" if query.get("watch", ["false"])[0].lower() == "true" else ("list" if route.name is None else "get")
        elif method == "POST":
            verb = "create"
        elif method in ("PUT", "PATCH"):
            verb = "update" if method == "PUT" else "patch"
        else:
            verb = "delete" if route.name else "deletecollection"
        attrs = RequestAttributes(
            verb=verb, group=route.group, resource=route.plural,
            namespace=route.namespace, name=route.name, subresource=subresource, path=route.path,
        )
        if not server.authorizer.authorize(user, attrs):
            raise forbidden(
                f'{route.plural} "{route.name or ""}" is forbidden: User "{user.name}" cannot '
                f'{verb} resource "{route.plural}" in API group "{route.group}"'
                + (f' in the namespace "{route.namespace}"' if route.namespace else "")
            )

    # ------------------------------------------------------------- resource ops
    def _handle_resource(self, route: Route, user: User, query: dict[str, list[str]]) -> None:
        server = self.api_server
        self._require_kind_scope(route)
        definition = server.registry.get(route.group, route.version, route.plural)
        review_kinds = {
            "SelfSubjectAccessReview", "SubjectAccessReview", "LocalSubjectAccessReview",
            "SelfSubjectRulesReview", "TokenReview",
        }
        if definition.kind in review_kinds:
            if self.command != "POST":
                raise ApiError(405, "MethodNotAllowed", "access reviews support POST only")
            if definition.kind == "SelfSubjectRulesReview":
                self._rules_review(route, user)
            elif definition.kind == "TokenReview":
                self._token_review(route)
            else:
                self._access_review(route, user if definition.kind == "SelfSubjectAccessReview" else None)
            return
        if route.name is None and self.command not in ("GET", "POST", "DELETE"):
            raise bad_request("a resource name is required for this operation")
        if route.name is None and self.command == "POST":
            self._create(route, definition, user, query)
            return
        if route.name is None and self.command == "DELETE":
            self._delete_collection(route, definition, query)
            return
        if route.name is None and self.command == "GET":
            if query.get("watch", ["false"])[0].lower() == "true":
                self._watch(route, definition, query)
                return
            limit_raw = (query.get("limit") or [None])[0]
            limit = int(limit_raw) if limit_raw else None
            items, token = server.store.list(
                definition.gvr, route.namespace,
                label_selector=(query.get("labelSelector") or [None])[0],
                field_selector=(query.get("fieldSelector") or [None])[0],
                limit=limit,
                continue_token=(query.get("continue") or [None])[0],
            )
            metadata = {"resourceVersion": str(server.store.resource_version)}
            if token:
                metadata["continue"] = token
            self._send_json(200, {
                "apiVersion": definition.api_version,
                "kind": definition.list_kind,
                "metadata": metadata,
                "items": items,
            })
            return
        assert route.name is not None
        if route.subresource:
            self._subresource(route, definition, user, query)
            return
        if self.command == "GET":
            if query.get("watch", ["false"])[0].lower() == "true":
                self._watch(route, definition, query)
                return
            obj = server.store.get(definition.gvr, route.namespace or "", route.name)
            if obj is None:
                raise not_found(definition.kind, route.name)
            self._send_json(200, obj)
        elif self.command == "POST":
            raise ApiError(405, "MethodNotAllowed", "POST is only valid on a collection")
        elif self.command == "PUT":
            self._update(route, definition, user, query)
        elif self.command == "PATCH":
            self._patch(route, definition, user, query)
        elif self.command == "DELETE":
            self._delete(route, definition, query)
        else:
            raise ApiError(405, "MethodNotAllowed", f"method {self.command} not allowed")

    def _require_kind_scope(self, route: Route) -> None:
        if route.group == "authorization.k8s.io" or route.group == "authentication.k8s.io":
            return
        # Resolution itself raises a clean 404 for unknown types.

    def _drain_body(self) -> None:
        """Consume an unread request body before responding.

        Leaving bytes in the socket makes some clients (notably on Windows)
        abort the connection instead of reading the error response.
        """
        if getattr(self, "_body_read", True):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _read_body(self) -> Any:
        self._body_read = True
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 16 * 1024 * 1024:
            raise ApiError(413, "RequestEntityTooLarge", "request body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        try:
            if content_type in ("application/yaml", "application/apply-patch+yaml", "text/yaml"):
                return yaml.safe_load(raw.decode("utf-8")) or {}
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise bad_request(f"unable to decode request body: {exc}") from exc

    def _create(self, route: Route, definition, user: User, query: dict[str, list[str]]) -> None:
        body = self._read_body()
        if not isinstance(body, dict):
            raise bad_request("request body must be a single object")
        if body.get("kind") and body.get("kind") != definition.kind:
            raise bad_request(f"kind {body.get('kind')!r} does not match resource kind {definition.kind!r}")
        body.setdefault("apiVersion", definition.api_version)
        body.setdefault("kind", definition.kind)
        meta = body.setdefault("metadata", {})
        # Server-owned metadata is never accepted from clients.  In particular,
        # a client cannot create an object that is already terminating.
        meta.pop("managedFields", None)
        meta.pop("deletionTimestamp", None)
        meta.pop("deletionGracePeriodSeconds", None)
        if definition.namespaced:
            if not route.namespace and not meta.get("namespace"):
                raise bad_request("namespace is required; use /api/v1/namespaces/<ns>/<resource>")
            meta["namespace"] = route.namespace or meta.get("namespace") or "default"
        else:
            meta.pop("namespace", None)
        request = AdmissionRequest("CREATE", definition.gvr, definition.kind, body,
                                   namespace=route.namespace, name=meta.get("name"),
                                   username=user.name, groups=user.groups)
        admitted = self.api_server.admission.admit(request)
        if query.get("dryRun", [""])[0] == "All":
            admitted["metadata"].setdefault("uid", "dry-run")
            self._send_json(201, admitted)
            return
        obj = self.api_server.store.create(definition.gvr, admitted, kind=definition.kind,
                                           namespace=route.namespace if definition.namespaced else "")
        self._send_json(201, obj)

    def _update(self, route: Route, definition, user: User, query: dict[str, list[str]]) -> None:
        body = self._read_body()
        if not isinstance(body, dict):
            raise bad_request("request body must be an object")
        meta = body.setdefault("metadata", {})
        if meta.get("name") and meta["name"] != route.name:
            raise bad_request("the name in the request body must match the URL")
        meta["name"] = route.name
        if definition.namespaced:
            meta["namespace"] = route.namespace or meta.get("namespace") or "default"
        body.setdefault("apiVersion", definition.api_version)
        body.setdefault("kind", definition.kind)
        old_obj = self.api_server.store.get(definition.gvr, route.namespace or "", route.name)
        if old_obj is not None and not meta.get("resourceVersion"):
            raise ApiError(422, "Invalid",
                           "metadata.resourceVersion: Invalid value: 0x0: must be specified for an update")
        if old_obj is not None and definition.has_status:
            # The main resource endpoint never writes status; that is the job
            # of the /status subresource.
            body["status"] = old_obj.get("status", {})
        if old_obj is not None:
            managed = old_obj.get("metadata", {}).get("managedFields")
            if managed:
                meta["managedFields"] = deep_copy(managed)
            else:
                meta.pop("managedFields", None)
        request = AdmissionRequest("UPDATE", definition.gvr, definition.kind, body,
                                   old_obj=old_obj, namespace=route.namespace, name=route.name,
                                   username=user.name, groups=user.groups)
        admitted = self.api_server.admission.admit(request)
        if query.get("dryRun", [""])[0] == "All":
            self._send_json(200, admitted)
            return
        obj = self.api_server.store.update(definition.gvr, admitted, kind=definition.kind,
                                           namespace=route.namespace if definition.namespaced else "")
        self._send_json(200, obj)

    def _patch(self, route: Route, definition, user: User, query: dict[str, list[str]]) -> None:
        body = self._read_body()
        content_type = (self.headers.get("Content-Type") or "application/merge-patch+json").split(";")[0]
        if content_type == APPLY_CONTENT_TYPE:
            self._server_side_apply(route, definition, user, query, body)
            return
        old = self.api_server.store.get(definition.gvr, route.namespace or "", route.name)
        if old is None:
            raise not_found(definition.kind, route.name)
        if content_type == "application/json-patch+json":
            patched = json_patch(old, body)
        else:
            patched = merge_patch(old, body)
        patched.setdefault("metadata", {})["name"] = route.name
        if definition.namespaced:
            patched["metadata"]["namespace"] = route.namespace or patched["metadata"].get("namespace", "default")
        if definition.has_status:
            patched["status"] = old.get("status", {})
        managed = old.get("metadata", {}).get("managedFields")
        if managed:
            patched["metadata"]["managedFields"] = deep_copy(managed)
        else:
            patched["metadata"].pop("managedFields", None)
        request = AdmissionRequest("UPDATE", definition.gvr, definition.kind, patched, old_obj=old,
                                   namespace=route.namespace, name=route.name,
                                   username=user.name, groups=user.groups)
        admitted = self.api_server.admission.admit(request)
        if query.get("dryRun", [""])[0] == "All":
            self._send_json(200, admitted)
            return
        obj = self.api_server.store.update(definition.gvr, admitted, kind=definition.kind,
                                           namespace=route.namespace if definition.namespaced else "")
        self._send_json(200, obj)

    def _server_side_apply(self, route: Route, definition, user: User,
                           query: dict[str, list[str]], body: Any) -> None:
        if not isinstance(body, dict):
            raise bad_request("apply patch must be a single object")
        manager = (query.get("fieldManager") or [""])[0]
        if not manager:
            raise bad_request("fieldManager is required for server-side apply")
        force = (query.get("force") or ["false"])[0].lower() in ("true", "1")
        old = self.api_server.store.get(definition.gvr, route.namespace or "", route.name)
        patch = deep_copy(body)
        if patch.get("kind") and patch["kind"] != definition.kind:
            raise bad_request(f"kind {patch['kind']!r} does not match resource kind {definition.kind!r}")
        patch.setdefault("metadata", {})["name"] = route.name
        if definition.namespaced:
            patch["metadata"]["namespace"] = route.namespace or patch["metadata"].get("namespace", "default")
        else:
            patch["metadata"].pop("namespace", None)
        merged = apply_object(
            old, patch, manager=manager, force=force,
            api_version=definition.api_version, kind=definition.kind,
            namespace=route.namespace or ("" if not definition.namespaced else "default"),
        )
        if old is None:
            request = AdmissionRequest("CREATE", definition.gvr, definition.kind, merged,
                                       namespace=route.namespace, name=route.name,
                                       username=user.name, groups=user.groups)
            admitted = self.api_server.admission.admit(request)
            if query.get("dryRun", [""])[0] == "All":
                admitted.setdefault("metadata", {}).setdefault("uid", "dry-run")
                self._send_json(201, admitted)
                return
            obj = self.api_server.store.create(definition.gvr, admitted, kind=definition.kind,
                                               namespace=route.namespace if definition.namespaced else "")
            self._send_json(201, obj)
            return
        merged.setdefault("metadata", {})["resourceVersion"] = old["metadata"]["resourceVersion"]
        request = AdmissionRequest("UPDATE", definition.gvr, definition.kind, merged, old_obj=old,
                                   namespace=route.namespace, name=route.name,
                                   username=user.name, groups=user.groups)
        admitted = self.api_server.admission.admit(request)
        if query.get("dryRun", [""])[0] == "All":
            self._send_json(200, admitted)
            return
        obj = self.api_server.store.update(definition.gvr, admitted, kind=definition.kind,
                                           namespace=route.namespace if definition.namespaced else "")
        self._send_json(200, obj)

    def _delete(self, route: Route, definition, query: dict[str, list[str]]) -> None:
        obj = self.api_server.store.get(definition.gvr, route.namespace or "", route.name)
        if obj is None:
            raise not_found(definition.kind, route.name)
        removed = self.api_server.store.delete(definition.gvr, route.namespace or "", route.name,
                                               kind=definition.kind)
        if removed:
            self._send_json(200, obj)
        else:
            self._send_json(200, self.api_server.store.get(definition.gvr, route.namespace or "", route.name) or obj)

    def _delete_collection(self, route: Route, definition, query: dict[str, list[str]]) -> None:
        """DELETE on a collection, honoring selectors like a real API server.

        The response is a Status object.  Objects with finalizers receive a
        deletionTimestamp and remain readable until their finalizers clear;
        they are still counted as requested deletions, exactly as the real
        deletecollection endpoint does.
        """
        namespace = route.namespace or ("" if not definition.namespaced else "default")
        items, _ = self.api_server.store.list(
            definition.gvr, namespace,
            label_selector=(query.get("labelSelector") or [None])[0],
            field_selector=(query.get("fieldSelector") or [None])[0],
        )
        deleted = 0
        for obj in items:
            name = obj["metadata"]["name"]
            try:
                self.api_server.store.delete(definition.gvr, namespace, name, kind=definition.kind)
                deleted += 1
            except ApiError:
                raise
            except Exception:
                continue
        self._send_json(200, {
            "apiVersion": "v1", "kind": "Status", "status": "Success",
            "details": {"name": "", "group": definition.group, "kind": definition.plural,
                        "uid": "", "causes": []},
            "deleted": deleted,
        })

    # --------------------------------------------------------------- subresource
    def _subresource(self, route: Route, definition, user: User, query: dict[str, list[str]]) -> None:
        sub = route.subresource
        assert sub is not None
        if sub == "status":
            self._status(route, definition, user)
        elif sub == "scale":
            self._scale(route, definition, user)
        elif sub == "finalize" and definition.kind == "Namespace":
            self._finalize(route, definition, user)
        elif sub == "log" and definition.kind == "Pod":
            self._logs(route)
        elif sub == "exec" and definition.kind == "Pod":
            self._exec(route, user)
        elif sub == "binding" and definition.kind == "Pod":
            self._binding(route, definition, user)
        elif sub == "eviction" and definition.kind == "Pod":
            self._eviction(route, definition, user)
        elif sub == "token" and definition.kind == "ServiceAccount":
            self._token(route, definition, user)
        elif sub in ("proxy",) and definition.kind in ("Service", "Pod"):
            self._proxy(route, definition)
        elif definition.kind == "SelfSubjectAccessReview":
            self._access_review(route, user)
        elif definition.kind in ("SubjectAccessReview", "LocalSubjectAccessReview"):
            self._access_review(route, None)
        elif definition.kind == "SelfSubjectRulesReview":
            self._rules_review(route, user)
        elif definition.kind == "TokenReview":
            self._token_review(route)
        else:
            raise not_found("subresource", sub)

    def _status(self, route: Route, definition, user: User) -> None:
        if not definition.has_status:
            raise not_found("subresource", "status")
        if self.command == "GET":
            obj = self.api_server.store.must_get(definition.gvr, route.namespace or "", route.name, definition.kind)
            self._send_json(200, {"apiVersion": obj["apiVersion"], "kind": obj["kind"],
                                  "metadata": obj["metadata"], "status": obj.get("status", {})})
            return
        if self.command not in ("PUT", "PATCH"):
            raise ApiError(405, "MethodNotAllowed", "status subresource supports GET, PUT and PATCH")
        old = self.api_server.store.must_get(definition.gvr, route.namespace or "", route.name, definition.kind)
        body = self._read_body()
        if self.command == "PATCH":
            patch_body = body.get("status", body) if isinstance(body, dict) else body
            status = merge_patch(old.get("status", {}), patch_body)
        else:
            status = body.get("status", body)
        updated = self.api_server.store.update_status(definition.gvr, route.namespace or "", route.name,
                                                      status, kind=definition.kind)
        self._send_json(200, updated)

    def _scale(self, route: Route, definition, user: User) -> None:
        if not definition.has_scale:
            raise not_found("subresource", "scale")
        old = self.api_server.store.must_get(definition.gvr, route.namespace or "", route.name, definition.kind)
        if self.command == "GET":
            self._send_json(200, self._scale_object(old, definition))
            return
        if self.command not in ("PUT", "PATCH"):
            raise ApiError(405, "MethodNotAllowed", "scale supports GET, PUT and PATCH")
        body = self._read_body()
        replicas = body.get("spec", {}).get("replicas", body.get("replicas"))
        if not isinstance(replicas, int) or replicas < 0:
            raise bad_request("scale spec.replicas must be a non-negative integer")
        old["spec"]["replicas"] = replicas
        updated = self.api_server.store.update(definition.gvr, old, kind=definition.kind,
                                               namespace=route.namespace or "")
        self._send_json(200, self._scale_object(updated, definition))

    @staticmethod
    def _scale_object(obj: dict, definition) -> dict:
        spec = obj.get("spec", {})
        selector = spec.get("selector", {}).get("matchLabels", {})
        return {
            "apiVersion": "autoscaling/v1",
            "kind": "Scale",
            "metadata": {"name": obj["metadata"]["name"], "namespace": obj["metadata"].get("namespace", ""),
                         "resourceVersion": obj["metadata"].get("resourceVersion"),
                         "uid": obj["metadata"].get("uid")},
            "spec": {"replicas": spec.get("replicas", 0)},
            "status": {"replicas": obj.get("status", {}).get("replicas", 0),
                       "selector": ",".join(f"{k}={v}" for k, v in selector.items())},
        }

    def _finalize(self, route: Route, definition, user: User) -> None:
        if self.command not in ("PUT", "PATCH"):
            raise ApiError(405, "MethodNotAllowed", "finalize supports PUT and PATCH")
        body = self._read_body()
        obj = self.api_server.store.must_get(definition.gvr, "", route.name, definition.kind)
        if self.command == "PATCH":
            obj = merge_patch(obj, body)
        else:
            obj["spec"] = body.get("spec", obj.get("spec", {}))
            if "metadata" in body:
                obj["metadata"].update(body["metadata"])
        obj["metadata"]["name"] = route.name
        obj["metadata"].pop("namespace", None)
        updated = self.api_server.store.update(definition.gvr, obj, kind=definition.kind, namespace="")
        self._send_json(200, updated)

    # ------------------------------------------------------------------ pod ops
    def _logs(self, route: Route) -> None:
        if self.command != "GET":
            raise ApiError(405, "MethodNotAllowed", "logs support GET")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pod = self.api_server.store.must_get(("", "v1", "pods"), route.namespace or "default", route.name, "Pod")
        container = (query.get("container") or [None])[0]
        if not container:
            containers = pod.get("spec", {}).get("containers", [])
            if len(containers) != 1:
                raise bad_request("a container name must be specified when the pod has multiple containers")
            container = containers[0].get("name")
        tail = int((query.get("tailLines") or ["200"])[0])
        previous = (query.get("previous") or ["false"])[0].lower() == "true"
        if self.api_server.log_provider is None:
            self._send_text(404, "container log backend is not configured\n")
            return
        text = self.api_server.log_provider(route.namespace or "default", route.name, container, tail, previous)
        self._send_text(200, text)

    def _exec(self, route: Route, user: User) -> None:
        if self.command != "POST":
            raise ApiError(405, "MethodNotAllowed", "exec supports POST")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        command = query.get("command") or []
        container = (query.get("container") or [""])[0]
        if self.api_server.exec_provider is None:
            result = {"stdout": "", "stderr": "exec backend is not configured\n", "exitCode": 1}
        else:
            result = self.api_server.exec_provider(route.namespace or "default", route.name, container, command)
        status = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success",
                  "stdout": result.get("stdout", ""), "stderr": result.get("stderr", "")}
        if result.get("exitCode"):
            status["status"] = "Failure"
            status["reason"] = "NonZeroExitCode"
            status["details"] = {"causes": [{"reason": "ExitCode", "message": str(result["exitCode"])}]}
        self._send_json(200, status)

    def _binding(self, route: Route, definition, user: User) -> None:
        if self.command != "POST":
            raise ApiError(405, "MethodNotAllowed", "binding supports POST")
        body = self._read_body()
        node_name = body.get("target", {}).get("name")
        if not node_name:
            raise bad_request("binding target.name is required")
        node = self.api_server.store.get(("", "v1", "nodes"), "", node_name)
        if node is None:
            raise not_found("node", node_name)
        pod = self.api_server.store.must_get(definition.gvr, route.namespace or "default", route.name, "Pod")
        pod["spec"]["nodeName"] = node_name
        self.api_server.store.update(definition.gvr, pod, kind="Pod", namespace=route.namespace or "default")
        self._send_json(201, {"kind": "Status", "apiVersion": "v1", "status": "Success"})

    def _eviction(self, route: Route, definition, user: User) -> None:
        if self.command != "POST":
            raise ApiError(405, "MethodNotAllowed", "eviction supports POST")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        namespace = route.namespace or "default"
        pod = self.api_server.store.must_get(definition.gvr, namespace, route.name, "Pod")
        pdb_list = self.api_server.store.list(("policy", "v1", "poddisruptionbudgets"), namespace)[0]
        matching: list[dict] = []
        for pdb in pdb_list:
            if not _selector_matches_pod(pdb.get("spec", {}).get("selector"), pod):
                continue
            status = pdb.get("status", {})
            policy = pdb.get("spec", {}).get("unhealthyPodEvictionPolicy", "IfHealthyBudget")
            # IfHealthyBudget also protects unhealthy Pods once the budget is
            # exhausted; AlwaysAllow lets them go first.
            if int(status.get("disruptionsAllowed", 1)) < 1 and (_pod_ready(pod) or policy == "IfHealthyBudget"):
                raise ApiError(429, "TooManyRequests",
                               f"cannot evict pod as it would violate the pod's disruption budget ({pdb['metadata']['name']})")
            matching.append(pdb)
        if query.get("dryRun", [""])[0] == "All":
            self._send_json(201, {"kind": "Status", "apiVersion": "v1", "status": "Success", "code": 201})
            return
        # Spend the budget synchronously, so eviction is safe even when the
        # disruption controller is temporarily stopped.  The controller later
        # recomputes the status from actual Pod readiness.
        for pdb in matching:
            status = deep_copy(pdb.get("status", {}))
            status["disruptionsAllowed"] = max(0, int(status.get("disruptionsAllowed", 1)) - 1)
            disrupted = dict(status.get("disruptedPods", {}))
            disrupted[route.name] = now_iso()
            status["disruptedPods"] = disrupted
            try:
                self.api_server.store.update_status(
                    ("policy", "v1", "poddisruptionbudgets"), namespace,
                    pdb["metadata"]["name"], status, kind="PodDisruptionBudget",
                )
            except ApiError:
                pass
        self.api_server.store.delete(definition.gvr, namespace, route.name, kind="Pod",
                                     propagation="foreground")
        self._send_json(201, {"kind": "Status", "apiVersion": "v1", "status": "Success", "code": 201})

    def _token(self, route: Route, definition, user: User) -> None:
        if self.command != "POST":
            raise ApiError(405, "MethodNotAllowed", "token supports POST")
        if self.api_server.token_issuer is None:
            raise ApiError(501, "NotImplemented", "service account token issuer is not configured")
        body = self._read_body()
        sa = self.api_server.store.must_get(("", "v1", "serviceaccounts"), route.namespace or "default",
                                            route.name, "ServiceAccount")
        audiences = body.get("spec", {}).get("audiences", ["https://kubernetes.default.svc.cluster.local"])
        expiration = int(body.get("spec", {}).get("expirationSeconds", 3600))
        issued = self.api_server.token_issuer.issue(route.namespace or "default", route.name,
                                                     audiences=audiences, expiration_seconds=expiration)
        self._send_json(201, {
            "apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
            "metadata": {"name": sa["metadata"]["name"], "namespace": sa["metadata"]["namespace"]},
            "spec": {"audiences": audiences, "expirationSeconds": expiration},
            "status": {"token": issued["token"], "expirationTimestamp": issued["expirationTimestamp"]},
        })

    def _proxy(self, route: Route, definition) -> None:
        if self.command not in ("GET", "POST"):
            raise ApiError(405, "MethodNotAllowed", "proxy supports GET and POST")
        self._send_json(200, {
            "kind": "Status", "apiVersion": "v1", "status": "Success",
            "message": f"proxied to {definition.kind.lower()}/{route.namespace or ''}/{route.name}",
        })

    # ------------------------------------------------------------- access review
    def _access_review(self, route: Route, user: User | None) -> None:
        body = self._read_body()
        spec = body.get("spec", {})
        if user is None:
            reviewed = User(spec.get("user", "system:anonymous"), list(spec.get("groups", [])))
        else:
            reviewed = user
        attrs_obj = spec.get("resourceAttributes") or {}
        attrs = RequestAttributes(
            verb=attrs_obj.get("verb", ""),
            group=attrs_obj.get("group", ""),
            resource=attrs_obj.get("resource", ""),
            namespace=attrs_obj.get("namespace"),
            name=attrs_obj.get("name"),
            subresource=attrs_obj.get("subresource"),
            path=spec.get("nonResourceAttributes", {}).get("path"),
            non_resource=bool(spec.get("nonResourceAttributes")),
        )
        allowed = self.api_server.authorizer.can_i(reviewed, attrs) if self.api_server.authorizer else True
        body["status"] = {"allowed": allowed, "reason": "RBAC: allowed" if allowed else "RBAC: denied"}
        if user is not None:
            body.setdefault("apiVersion", "authorization.k8s.io/v1")
            body.setdefault("kind", "SelfSubjectAccessReview")
        self._send_json(201, body)

    def _rules_review(self, route: Route, user: User) -> None:
        body = self._read_body()
        body["status"] = {"resourceRules": [], "nonResourceRules": [], "incomplete": True}
        self._send_json(201, body)

    def _token_review(self, route: Route) -> None:
        body = self._read_body()
        spec = body.get("spec", {})
        token = spec.get("token", "")
        audiences = [a for a in (spec.get("audiences") or []) if a]
        status: dict[str, Any] = {"authenticated": False}
        if self.api_server.token_issuer and token:
            try:
                if audiences:
                    # Audience-aware verification: the token must be valid
                    # for at least one of the audiences requested by the
                    # resource server.
                    user = None
                    last_error: ApiError | None = None
                    for audience in audiences:
                        try:
                            user = self.api_server.token_issuer.validate(token, audience=audience)
                            break
                        except ApiError as exc:
                            last_error = exc
                    if user is None:
                        raise last_error or unauthorized("service account token audience mismatch")
                else:
                    user = self.api_server.token_issuer.validate(token)
                status = {"authenticated": True, "user": {"username": user.name, "groups": user.groups,
                                                          "uid": f"sa-{user.name}"},
                          "audiences": audiences}
            except ApiError:
                status = {"authenticated": False, "error": "token is invalid or expired"}
        body["status"] = status
        self._send_json(201, body)

    # --------------------------------------------------------------------- watch
    def _watch(self, route: Route, definition, query: dict[str, list[str]]) -> None:
        rv = (query.get("resourceVersion") or [None])[0]
        timeout = min(int((query.get("timeoutSeconds") or ["60"])[0]), 300)
        selector = (query.get("labelSelector") or [None])[0]
        field_selector = (query.get("fieldSelector") or [None])[0]
        bookmarks = (query.get("allowWatchBookmarks") or ["false"])[0].lower() in ("true", "1")
        # resourceVersion=0 means "start from the current state": the watch
        # cache sends the existing objects as ADDED events.
        send_initial = rv == "0"
        watcher = self.api_server.store.watch(
            definition.gvr, route.namespace, resource_version=rv, send_initial=send_initial
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        deadline = time.time() + timeout

        def write_chunk(payload: bytes) -> None:
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        if bookmarks:
            bookmark = {
                "apiVersion": definition.api_version,
                "kind": definition.kind,
                "metadata": {"resourceVersion": str(self.api_server.store.resource_version)},
            }
            write_chunk(json.dumps({"type": "BOOKMARK", "object": bookmark}).encode() + b"\n")

        seen_match: dict[tuple[str, str], bool] = {}
        if selector or field_selector:
            # Remember objects that already match at watch start so leaving
            # the selection later produces a synthetic DELETED.
            from mini_k8s.apimachinery.selectors import match_field_selector
            for existing in self.api_server.store.list(definition.gvr, route.namespace)[0]:
                labels_ok = not selector or match_labels(existing.get("metadata", {}).get("labels"), selector)
                fields_ok = not field_selector or match_field_selector(existing, field_selector)
                if labels_ok and fields_ok:
                    meta = existing.get("metadata", {})
                    seen_match[(meta.get("namespace", ""), meta.get("name", ""))] = True
        try:
            while time.time() < deadline:
                try:
                    event = watcher.queue.get(timeout=min(0.5, max(0.01, deadline - time.time())))
                except Exception:
                    continue
                if event is None:
                    break
                if event.type == "ERROR":
                    write_chunk(json.dumps({"type": "ERROR", "object": event.object}).encode() + b"\n")
                    break
                obj = event.object
                event_type = event.type
                if selector or field_selector:
                    from mini_k8s.apimachinery.selectors import match_field_selector
                    labels_ok = not selector or match_labels(obj.get("metadata", {}).get("labels"), selector)
                    fields_ok = not field_selector or match_field_selector(obj, field_selector)
                    matches = labels_ok and fields_ok
                    meta = obj.get("metadata", {})
                    key = (meta.get("namespace", ""), meta.get("name", ""))
                    previous = seen_match.get(key, False)
                    if matches and not previous:
                        event_type = "ADDED"
                    elif not matches and previous:
                        event_type = "DELETED"
                    elif not matches:
                        seen_match[key] = False
                        continue
                    if event.type == "DELETED":
                        seen_match.pop(key, None)
                    else:
                        seen_match[key] = matches
                line = json.dumps({"type": event_type, "object": obj}, ensure_ascii=False).encode() + b"\n"
                write_chunk(line)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            watcher.close()

    # ------------------------------------------------------------------ metrics
    def _metrics(self) -> str:
        lines = ["# HELP mini_k8s_up Whether the MiniK8s API server is running.", "# TYPE mini_k8s_up gauge",
                 "mini_k8s_up 1", "# HELP mini_k8s_resource_version Current global resource version.",
                 "# TYPE mini_k8s_resource_version gauge", f"mini_k8s_resource_version {self.api_server.store.resource_version}"]
        for gvr in (("", "v1", "pods"), ("", "v1", "nodes"), ("apps", "v1", "deployments"),
                    ("apps", "v1", "replicasets"), ("batch", "v1", "jobs"), ("", "v1", "services")):
            lines.append(f'mini_k8s_objects{{resource="{gvr[2]}"}} {self.api_server.store.count(gvr)}')
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ outputs
    def _send_json(self, status: int, body: Any) -> None:
        self._drain_body()
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, status: int, text: str) -> None:
        self._drain_body()
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _pod_ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", []))


def _match_expressions(expressions: list[dict], labels: dict[str, str]) -> bool:
    from mini_k8s.apimachinery.selectors import match_expression
    return all(match_expression(expression, labels) for expression in expressions)


def _selector_matches_pod(selector: dict | None, pod: dict) -> bool:
    if not selector:
        return False
    labels = pod.get("metadata", {}).get("labels", {})
    match_labels_map = selector.get("matchLabels", {})
    if match_labels_map and not all(labels.get(key) == value for key, value in match_labels_map.items()):
        return False
    expressions = selector.get("matchExpressions", [])
    if expressions and not _match_expressions(expressions, labels):
        return False
    return bool(match_labels_map or expressions)
