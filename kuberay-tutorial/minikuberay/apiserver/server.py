"""A small Kubernetes-compatible REST server for MiniKubeRay objects."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import constants as C
from ..errors import ApiError, BadRequest, Forbidden, Invalid, NotFound, Unauthorized
from ..history import HistoryServer
from ..metrics import MetricsRegistry
from ..models.defaults import apply_defaults
from ..models.validation import validate_object
from ..store.store import ApiStore
from .dashboard import DASHBOARD_HTML

API_GROUP_PREFIX = {
    C.KIND_POD: ("", "v1"),
    C.KIND_SERVICE: ("", "v1"),
    C.KIND_EVENT: ("", "v1"),
    C.KIND_SECRET: ("", "v1"),
    C.KIND_PVC: ("", "v1"),
    C.KIND_SERVICEACCOUNT: ("", "v1"),
    C.KIND_JOB: ("batch", "v1"),
    C.KIND_ENDPOINTSLICE: ("discovery.k8s.io", "v1"),
    C.KIND_NETWORKPOLICY: ("networking.k8s.io", "v1"),
    C.KIND_HTTPROUTE: ("gateway.networking.k8s.io", "v1"),
    C.KIND_GATEWAY: ("gateway.networking.k8s.io", "v1"),
    C.KIND_PODGROUP: ("scheduling.volcano.sh", "v1beta1"),
    C.KIND_INGRESS: ("networking.k8s.io", "v1"),
}


class ApiServer:
    def __init__(
        self,
        store: ApiStore,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str | None = None,
        read_only: bool = False,
        runtime=None,
        metrics: MetricsRegistry | None = None,
        history: HistoryServer | None = None,
        tracer=None,
    ):
        self.store = store
        self.host = host
        self.port = port
        self.token = token
        self.read_only = read_only
        self.runtime = runtime
        self.metrics = metrics or MetricsRegistry()
        self.history = history
        self.tracer = tracer
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        port = self._httpd.server_address[1] if self._httpd else self.port
        return f"http://{self.host}:{port}"

    def start(self) -> str:
        if self._httpd:
            return self.url
        server = self

        class Handler(_Handler):
            api_server = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="minikuberay-apiserver", daemon=True)
        self._thread.start()
        return self.url

    def serve_forever(self) -> None:
        server = self

        class Handler(_Handler):
            api_server = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        print(f"MiniKubeRay API server listening on {self.url}")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._httpd.server_close()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


class _Handler(BaseHTTPRequestHandler):
    api_server: ApiServer
    protocol_version = "HTTP/1.1"
    server_version = "MiniKubeRayAPIServer/1.7"

    # ------------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args) -> None:
        # Keep the terminal usable for demos.
        if getattr(self.api_server, "verbose", False):
            super().log_message(fmt, *args)

    def _authorized(self) -> None:
        token = self.api_server.token
        if not token:
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/healthz", "/readyz", "/metrics", "/version", "/dashboard", "/dashboard/"):
            return
        if path == "/api/traces" or path.startswith("/api/traces/"):
            return
        header = self.headers.get("Authorization", "")
        import hmac

        if not hmac.compare_digest(header, f"Bearer {token}"):
            raise Unauthorized("missing or invalid bearer token")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        self._body_read = True
        if length <= 0:
            return {}
        if length > 8 * 1024 * 1024:
            raise BadRequest("request body is too large")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise BadRequest(f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    def _drain_unread_body(self) -> None:
        if getattr(self, "_body_read", False):
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 0:
            try:
                self.rfile.read(min(length, 8 * 1024 * 1024))
            except Exception:
                pass
            if length > 8 * 1024 * 1024:
                self.close_connection = True
        self._body_read = True

    def _normalize_body(self, body: dict, kind: str, namespace: str, name: str | None = None) -> None:
        body_kind = body.get("kind")
        if body_kind and body_kind != kind:
            raise BadRequest(f"body kind {body_kind!r} does not match path kind {kind!r}")
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            body["metadata"] = metadata
        body_name = metadata.get("name")
        if name and body_name and body_name != name:
            raise BadRequest(f"body name {body_name!r} does not match path name {name!r}")
        body_namespace = metadata.get("namespace")
        if body_namespace and body_namespace != namespace:
            raise BadRequest(f"body namespace {body_namespace!r} does not match path namespace {namespace!r}")
        metadata["name"] = name or body_name
        metadata["namespace"] = namespace
        body["kind"] = kind

    def _send(self, status: int, payload, *, content_type: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # Closing each connection avoids Windows keep-alive resets when a
        # client aborts a request (notably in tests that stop a server).
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
        self.wfile.flush()

    def _error(self, exc: ApiError) -> None:
        # Consume an unread request body before responding; otherwise a
        # keep-alive connection can be reset on Windows.
        self._drain_unread_body()
        self._send(exc.status, exc.to_dict())

    def _dispatch(self) -> None:
        try:
            self._authorized()
            path = urllib.parse.urlparse(self.path).path
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if path == "/healthz":
                return self._send(200, {"status": "ok"})
            if path == "/readyz":
                return self._send(200, {"status": "ok"})
            if path == "/version":
                return self._send(
                    200,
                    {
                        "major": "1",
                        "minor": "34",
                        "gitVersion": "v1.34.0-minikuberay",
                        "platform": "minikuberay",
                    },
                )
            if path == "/metrics":
                return self._send(200, self.api_server.metrics.render(), content_type="text/plain; version=0.0.4")
            if path in ("/dashboard", "/dashboard/"):
                return self._send(200, DASHBOARD_HTML, content_type="text/html; charset=utf-8")
            if path == "/openapi/v3":
                return self._send(200, {"paths": {}})
            if path == "/api":
                return self._send(200, {"kind": "APIVersions", "versions": ["v1"]})
            if path == "/apis":
                return self._send(
                    200,
                    {
                        "kind": "APIGroupList",
                        "apiVersion": "v1",
                        "groups": [
                            {
                                "name": "ray.io",
                                "versions": [{"groupVersion": "ray.io/v1", "version": "v1"}],
                                "preferredVersion": {"groupVersion": "ray.io/v1", "version": "v1"},
                            },
                            {
                                "name": "batch",
                                "versions": [{"groupVersion": "batch/v1", "version": "v1"}],
                                "preferredVersion": {"groupVersion": "batch/v1", "version": "v1"},
                            },
                        ],
                    },
                )
            if path == "/apis/ray.io/v1":
                return self._send(200, {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "ray.io/v1", "resources": [
                    {"name": plural, "kind": kind, "namespaced": True}
                    for plural, kind in C.PLURAL_TO_KIND.items()
                    if kind in C.RAY_KINDS
                ]})
            if path == "/api/v1/namespaces":
                namespaces = sorted({obj["metadata"].get("namespace", "default") for obj in self.api_server.store.list(C.KIND_POD, None)} | {"default"})
                return self._send(200, {"kind": "NamespaceList", "apiVersion": "v1", "items": [{"metadata": {"name": name}} for name in namespaces]})
            if path == "/api/dashboard/summary":
                return self._send(200, self._summary())
            if path == "/api/history/sessions":
                return self._send(200, {"sessions": self._history_sessions(query)})
            if path.startswith("/api/history/sessions/"):
                return self._history_route(path, query)
            if path == "/api/traces" or path.startswith("/api/traces/"):
                return self._traces_route(path, query)
            if path == "/api/v0/healthz":
                # Ray's State API health endpoint; the teaching subset keeps
                # the same envelope so clients can tell "server is up" from
                # "the cluster answered".
                return self._send(200, {"result": True, "msg": ""})
            if path.startswith("/api/v0/"):
                return self._state_route(path, query)

            parsed = self._parse_resource_path(path)
            if parsed is None:
                raise NotFound(f"the server could not find the requested resource: {path}")
            kind, namespace, name, subresource = parsed
            method = self.command
            dry_run = query.get("dryRun", [""])[0].lower() in ("all", "true")
            if method in ("POST", "PUT", "PATCH", "DELETE") and self.api_server.read_only:
                raise Forbidden("API server is running in read-only mode")
            if method == "GET":
                if name is None:
                    if subresource == "log":
                        raise BadRequest("a name is required for log requests")
                    if query.get("watch", ["false"])[0].lower() == "true":
                        return self._watch(kind, namespace, query)
                    selector = _parse_label_selector(query.get("labelSelector", [""])[0])
                    field_selector = _parse_label_selector(query.get("fieldSelector", [""])[0])
                    items = self.api_server.store.list(kind, namespace, label_selector=selector or None, field_selector=field_selector or None)
                    group, version = API_GROUP_PREFIX.get(kind, ("", "v1"))
                    api_version = f"{group}/{version}" if group else version
                    return self._send(
                        200,
                        {
                            "kind": f"{kind}List",
                            "apiVersion": C.RAY_API_VERSION if kind in C.RAY_KINDS else api_version,
                            "metadata": {"resourceVersion": str(self.api_server.store.resource_version)},
                            "items": items,
                        },
                    )
                if subresource in ("log", "logs"):
                    return self._pod_log(kind, namespace, name, query)
                if kind == C.KIND_RAYCLUSTER and subresource == "nodes":
                    return self._raycluster_nodes(namespace, name)
                if kind == C.KIND_RAYCLUSTER and subresource == "workergroups":
                    return self._raycluster_workergroups(namespace, name)
                obj = self.api_server.store.get(kind, name, namespace)
                return self._send(200, obj)
            if method == "POST":
                if subresource == "status":
                    raise BadRequest("status is a subresource; use PUT .../status")
                if name is not None and subresource == "stop":
                    # Consume the optional request body before responding;
                    # otherwise Windows can reset the connection.
                    self._drain_unread_body()
                    return self._stop_rayjob(namespace, name)
                body = self._read_body()
                body.pop("status", None)  # status is a subresource, never a create input
                self._normalize_body(body, kind, namespace)
                if kind in C.RAY_KINDS:
                    body = apply_defaults(body)
                    errors = validate_object(body)
                    if errors:
                        raise Invalid("; ".join(errors))
                if dry_run:
                    # Kubernetes returns the (defaulted) object without
                    # persisting it for dryRun=All.
                    return self._send(200, body)
                created = self.api_server.store.create(kind, body, namespace)
                return self._send(201, created)
            if method == "PUT":
                body = self._read_body()
                self._normalize_body(body, kind, namespace, name)
                if dry_run:
                    return self._send(200, body)
                if subresource == "status":
                    updated = self.api_server.store.update(kind, body, subresource="status")
                else:
                    body.pop("status", None)
                    if kind in C.RAY_KINDS:
                        body = apply_defaults(body)
                        errors = validate_object(body)
                        if errors:
                            raise Invalid("; ".join(errors))
                    updated = self.api_server.store.update(kind, body)
                return self._send(200, updated)
            if method == "PATCH":
                body = self._read_body()
                patch_type = "merge"
                content_type = self.headers.get("Content-Type", "")
                if "apply-patch" in content_type or "strategic" in content_type:
                    patch_type = "strategic"
                if dry_run:
                    current = self.api_server.store.get(kind, name, namespace)
                    if patch_type == "merge":
                        from ..models.meta import merge_patch

                        return self._send(200, merge_patch(current, body))
                    return self._send(200, current)
                updated = self.api_server.store.patch(
                    kind, name, namespace, body, patch_type=patch_type, subresource=subresource
                )
                return self._send(200, updated)
            if method == "DELETE":
                force = query.get("force", ["false"])[0].lower() == "true"
                if dry_run:
                    return self._send(200, self.api_server.store.get(kind, name, namespace))
                deleted = self.api_server.store.delete(kind, name, namespace, force=force)
                return self._send(200, deleted)
            raise BadRequest(f"unsupported method: {method}")
        except ApiError as exc:
            self._error(exc)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self._send(500, {"kind": "Status", "status": "Failure", "message": f"{type(exc).__name__}: {exc}", "code": 500})

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    # -------------------------------------------------------------------- routes
    def _parse_resource_path(self, path: str):
        parts = [part for part in path.split("/") if part]
        if not parts:
            return None
        namespace = None
        if parts[0] == "api":
            if len(parts) < 3:
                return None
            rest = parts[3:] if len(parts) >= 4 and parts[2] == "namespaces" else parts[2:]
            if len(parts) >= 4 and parts[2] == "namespaces":
                namespace = parts[3]
                rest = parts[4:]
            return self._resource_from_rest(rest, namespace)
        if parts[0] == "apis":
            if len(parts) < 4:
                return None
            group, version = parts[1], parts[2]
            rest = parts[3:]
            if rest and rest[0] == "namespaces":
                if len(rest) < 2:
                    return None
                namespace = rest[1]
                rest = rest[2:]
            return self._resource_from_rest(rest, namespace, group=group)
        return None

    @staticmethod
    def _resource_from_rest(rest: list[str], namespace: str | None, group: str | None = None):
        if not rest:
            return None
        plural = rest[0]
        kind = C.PLURAL_TO_KIND.get(plural)
        if not kind:
            return None
        if group is not None:
            if kind == C.KIND_PODGROUP:
                # The object's apiVersion depends on the configured scheduler
                # (Volcano, scheduler-plugins, ...).
                if group not in ("scheduling.volcano.sh", "scheduling.x-k8s.io", "scheduling.yunikorn.apache.org"):
                    return None
            else:
                expected_group, _ = API_GROUP_PREFIX.get(kind, (C.RAY_API_VERSION.split("/")[0], "v1"))
                if group not in (expected_group, "ray.io"):
                    return None
        name = rest[1] if len(rest) > 1 else None
        subresource = rest[2] if len(rest) > 2 else None
        return kind, namespace, name, subresource

    def _watch(self, kind: str, namespace: str | None, query: dict) -> None:
        selector = _parse_label_selector(query.get("labelSelector", [""])[0])
        try:
            since = int(query.get("resourceVersion", ["0"])[0] or 0)
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"invalid resourceVersion: {exc}") from exc
        # Check before writing the 200 header so a compacted RV can return 410.
        self.api_server.store.check_watch_resource_version(since)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for event in self.api_server.store.watch(kind, namespace, since_resource_version=since):
                if selector and not _selector_matches(selector, event.object.get("metadata", {}).get("labels") or {}):
                    continue
                payload = json.dumps({"type": event.type, "object": event.object}, ensure_ascii=False).encode("utf-8") + b"\n"
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        else:
            # Properly terminate a chunked response if the store closes.
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _raycluster_nodes(self, namespace: str, name: str) -> None:
        runtime = self.api_server.runtime
        nodes = [node.to_dict() for node in runtime.nodes(namespace, name)] if runtime else []
        return self._send(200, {"kind": "NodeList", "apiVersion": "ray.io/v1", "items": nodes})

    def _raycluster_workergroups(self, namespace: str, name: str) -> None:
        cluster = self.api_server.store.get(C.KIND_RAYCLUSTER, name, namespace)
        status = cluster.get("status") or {}
        items = []
        for group in (cluster.get("spec") or {}).get("workerGroupSpecs") or []:
            items.append(
                {
                    "name": group.get("groupName"),
                    "replicas": group.get("replicas", 0),
                    "minReplicas": group.get("minReplicas", 0),
                    "maxReplicas": group.get("maxReplicas"),
                    "numOfHosts": group.get("numOfHosts", 1),
                    "priority": group.get("priority", 0),
                    "image": ((group.get("template") or {}).get("spec", {}).get("containers") or [{}])[0].get("image", ""),
                }
            )
        return self._send(
            200,
            {
                "kind": "WorkerGroupList",
                "apiVersion": "ray.io/v1",
                "cluster": name,
                "desiredWorkerReplicas": status.get("desiredWorkerReplicas", 0),
                "readyWorkerReplicas": status.get("readyWorkerReplicas", 0),
                "items": items,
            },
        )

    def _pod_log(self, kind: str, namespace: str, name: str, query: dict) -> None:
        try:
            tail = int(query.get("tailLines", query.get("tail", ["200"])[0])[0] or 200)
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"invalid tailLines: {exc}") from exc
        if kind == C.KIND_POD:
            pod = self.api_server.store.get(kind, name, namespace)
            lines = [f"pod {name} phase={pod.get('status', {}).get('phase')}"]
            for status in pod.get("status", {}).get("containerStatuses") or []:
                lines.append(f"container {status.get('name')} ready={status.get('ready')}")
            return self._send(200, "\n".join(lines[-tail:]) + "\n", content_type="text/plain")
        if kind == C.KIND_RAYJOB:
            job = self.api_server.store.get(kind, name, namespace)
            cluster_name = (job.get("status") or {}).get("rayClusterName")
            job_id = (job.get("status") or {}).get("jobId")
            if self.api_server.runtime and cluster_name and job_id:
                lines = self.api_server.runtime.job_logs(namespace, cluster_name, job_id)
                return self._send(200, "\n".join(lines[-tail:]) + "\n", content_type="text/plain")
            return self._send(200, "no logs yet\n", content_type="text/plain")
        raise BadRequest("logs are only available for Pods and RayJobs")

    def _stop_rayjob(self, namespace: str, name: str) -> None:
        job = self.api_server.store.get(C.KIND_RAYJOB, name, namespace)
        cluster_name = (job.get("status") or {}).get("rayClusterName")
        job_id = (job.get("status") or {}).get("jobId")
        if self.api_server.runtime and cluster_name and job_id:
            self.api_server.runtime.stop_job(namespace, cluster_name, job_id)
        return self._send(200, {"status": "stopped", "jobId": job_id})

    def _history_sessions(self, query: dict) -> list[dict]:
        history = self.api_server.history
        if history is None:
            return []
        term = query.get("q", [""])[0]
        return history.search(term) if term else history.list_sessions()

    def _history_route(self, path: str, query: dict) -> None:
        history = self.api_server.history
        if history is None:
            raise NotFound("History Server is disabled")
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        # api / history / sessions / <namespace> / <cluster> [ / events|replay|snapshot ]
        if len(parts) < 5:
            raise BadRequest("history path must be /api/history/sessions/<namespace>/<cluster>")
        namespace, cluster = parts[3], parts[4]
        tail = parts[5] if len(parts) > 5 else ""
        if not tail:
            session = history.get_session(namespace, cluster)
            if session is None:
                raise NotFound(f"history session {namespace}/{cluster} not found")
            return self._send(200, session)
        if tail == "events":
            try:
                limit = int(query.get("limit", ["0"])[0] or 0)
            except (TypeError, ValueError) as exc:
                raise BadRequest(f"invalid limit: {exc}") from exc
            kind = query.get("kind", [None])[0]
            return self._send(200, {"events": history.events(namespace, cluster, limit=limit or None, kind=kind)})
        if tail == "snapshot":
            session = history.get_session(namespace, cluster)
            if session is None:
                raise NotFound(f"history session {namespace}/{cluster} not found")
            return self._send(200, session.get("snapshot") or {})
        if tail == "replay":
            replay = history.replay(namespace, cluster)
            if replay is None:
                raise NotFound(f"history session {namespace}/{cluster} not found")
            return self._send(200, replay)
        raise NotFound(f"unknown history subresource: {tail}")

    def _traces_route(self, path: str, query: dict) -> None:
        tracer = self.api_server.tracer
        if tracer is None:
            return self._send(200, {"enabled": False, "traces": [], "spans": [], "stats": {}})
        try:
            limit = int(query.get("limit", ["100"])[0] or 100)
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"invalid limit: {exc}") from exc
        limit = max(1, min(limit, 5000))
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        # api / traces
        if len(parts) == 2:
            return self._send(
                200,
                {
                    "enabled": True,
                    "stats": tracer.stats(),
                    "traces": tracer.traces(limit=min(limit, 200)),
                },
            )
        # api / traces / <trace_id>
        if len(parts) == 3:
            spans = tracer.spans(limit=limit, trace_id=parts[2])
            if not spans:
                raise NotFound(f"trace {parts[2]} not found")
            return self._send(200, {"trace_id": parts[2], "spans": spans})
        raise NotFound("traces path must be /api/traces or /api/traces/<trace_id>")

    # ------------------------------------------------------------------ state API
    def _state_route(self, path: str, query: dict) -> None:
        """A teaching subset of Ray's State API (``/api/v0/*``).

        The real State API is served by the Ray dashboard and covers tasks,
        actors, workers, objects and runtime envs as well.  MiniKubeRay models
        the control-plan boundary, so it exposes the four datasets it can
        answer honestly: nodes, jobs, Serve applications and Kubernetes
        events.  Keeping Ray's response envelope
        (``data.result.result`` / ``data.result.total``) means the same
        ``jq``/CLI-shaped queries work against both.
        """
        runtime = self.api_server.runtime
        namespace = (query.get("namespace", [None])[0] or "").strip() or None
        cluster = (query.get("cluster", [None])[0] or "").strip() or None
        term = (query.get("filter", [""])[0] or "").strip().lower()
        try:
            limit = int(query.get("limit", ["0"])[0] or 0)
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"invalid limit: {exc}") from exc
        if limit < 0:
            raise BadRequest("limit must be non-negative")

        route = path[len("/api/v0/"):].strip("/")
        snapshot = runtime.snapshot().get("clusters", {}) if runtime else {}
        items: list[dict] = []

        if route == "nodes":
            for key, data in snapshot.items():
                ns, name = key.split("/", 1)
                if namespace and ns != namespace:
                    continue
                if cluster and name != cluster:
                    continue
                for node in data.get("nodes") or []:
                    items.append(
                        {
                            **node,
                            "namespace": ns,
                            "cluster": name,
                            "state": "ALIVE" if node.get("alive") else "DEAD",
                        }
                    )
        elif route == "jobs":
            for key, data in snapshot.items():
                ns, name = key.split("/", 1)
                if namespace and ns != namespace:
                    continue
                if cluster and name != cluster:
                    continue
                for job in data.get("jobs") or []:
                    items.append({**job, "namespace": ns, "cluster": name})
        elif route == "serve/applications":
            for key, data in snapshot.items():
                ns, name = key.split("/", 1)
                if namespace and ns != namespace:
                    continue
                if cluster and name != cluster:
                    continue
                serve = data.get("serve")
                for app_name, app_status in ((serve or {}).get("statuses") or {}).items():
                    items.append(
                        {
                            "name": app_name,
                            "namespace": ns,
                            "cluster": name,
                            "status": app_status.get("status", ""),
                            "message": app_status.get("message", ""),
                            "deployments": app_status.get("serveDeploymentStatuses") or {},
                            "targetCapacity": (serve or {}).get("targetCapacity"),
                            "trafficRoutedPercent": (serve or {}).get("trafficRoutedPercent"),
                        }
                    )
        elif route == "events":
            for event in self.api_server.store.list(C.KIND_EVENT, namespace):
                involved = event.get("involvedObject") or {}
                target = str(involved.get("name") or "")
                # RayCluster events use the CR name; child-resource events use
                # names such as "<cluster>-head-xxxxx" or "<cluster>-workers-1".
                if cluster and target != cluster and not target.startswith(cluster + "-"):
                    continue
                items.append(event)
            items.sort(key=lambda item: item.get("lastTimestamp", ""), reverse=True)
        else:
            raise NotFound(f"the State API subset does not expose /api/v0/{route}")

        if term:
            import json as _json

            items = [item for item in items if term in _json.dumps(item, ensure_ascii=False, default=str).lower()]
        total = len(items)
        if limit:
            items = items[:limit]
        return self._send(
            200,
            {
                "result": True,
                "msg": "",
                "data": {
                    "result": {"result": items, "total": total},
                    "partial_failure_warning": "",
                },
            },
        )

    def _summary(self) -> dict:
        store = self.api_server.store
        history = self.api_server.history
        return {
            "rayclusters": len(store.list(C.KIND_RAYCLUSTER, None)),
            "rayjobs": len(store.list(C.KIND_RAYJOB, None)),
            "rayservices": len(store.list(C.KIND_RAYSERVICE, None)),
            "raycronjobs": len(store.list(C.KIND_RAYCRONJOB, None)),
            "pods": len(store.list(C.KIND_POD, None)),
            "historySessions": len(history.list_sessions()) if history else 0,
            "resourceVersion": store.resource_version,
        }


def _parse_label_selector(value: str) -> dict:
    if not value:
        return {}
    selector: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "!=" in item:
            key, val = item.split("!=", 1)
            selector[f"!{key}"] = val
        elif "=" in item:
            key, val = item.split("=", 1)
            selector[key.strip()] = val.strip()
        else:
            # Kubernetes labelSelector: a bare key means "exists".
            selector[f"__exists__:{item}"] = ""
    return selector


def _selector_matches(selector: dict, labels: dict) -> bool:
    for key, value in selector.items():
        if key.startswith("__exists__:"):
            if key.split(":", 1)[1] not in labels:
                return False
        elif key.startswith("!"):
            if labels.get(key[1:]) == value:
                return False
        elif labels.get(key) != value:
            return False
    return True
