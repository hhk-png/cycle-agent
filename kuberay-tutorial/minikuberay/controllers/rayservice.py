"""RayService reconciler: Serve deployment, blue/green and incremental upgrade."""

from __future__ import annotations

from datetime import timedelta

from .. import constants as C
from ..clock import iso, parse_iso
from ..models.core import new_service, pod_is_ready, service_port
from ..models.defaults import apply_defaults
from ..models.meta import (
    add_finalizer,
    deletion_timestamp,
    is_condition_true,
    name as obj_name,
    namespace as obj_namespace,
    owner_reference,
    remove_finalizer,
    set_condition,
)
from ..models.rayservice import (
    ServeConfigError,
    all_apps_ready,
    cluster_name_for_service,
    cluster_spec_hash,
    parse_serve_config,
    statuses_for_apps,
)
from ..models.validation import validate_object
from ..util import canonical_json, deep_copy, parse_duration_seconds, stable_hash
from .base import Controller
from .builders import build_gateway, build_httproute, head_service_ports


class RayServiceController(Controller):
    name = "rayservice"
    kind = C.KIND_RAYSERVICE

    # ---------------------------------------------------------------- lifecycle
    def reconcile(self, obj: dict) -> bool:
        service = deep_copy(obj)
        namespace = obj_namespace(service)
        name = obj_name(service)

        if deletion_timestamp(service):
            return self._cleanup(service)

        if (service.get("spec") or {}).get("managedBy") == C.MANAGED_BY_MULTIKUEUE:
            return False

        if add_finalizer(service, C.FINALIZER_RAYSERVICE):
            service["metadata"]["resourceVersion"] = obj["metadata"]["resourceVersion"]
            self.store.update(C.KIND_RAYSERVICE, service)
            return True

        defaulted = apply_defaults(service)
        if canonical_json(defaulted.get("spec")) != canonical_json(service.get("spec")):
            merged = deep_copy(service)
            merged["spec"] = defaulted["spec"]
            self.store.update(C.KIND_RAYSERVICE, merged)
            return True

        errors = validate_object(defaulted)
        if errors:
            return self._validation_failed(defaulted, "; ".join(errors))
        try:
            parse_serve_config(defaulted["spec"].get("serveConfigV2", ""))
        except ServeConfigError as exc:
            return self._validation_failed(defaulted, str(exc))
        if (
            (defaulted["spec"].get("upgradeStrategy") or {}).get("type") == "NewClusterWithIncrementalUpgrade"
            and not self.config.enabled("RayServiceIncrementalUpgrade")
        ):
            return self._validation_failed(defaulted, "incremental upgrade requires the RayServiceIncrementalUpgrade feature gate")

        if defaulted.get("spec", {}).get("managedBy") == C.MANAGED_BY_MULTIKUEUE:
            return False

        if defaulted["spec"].get("suspend"):
            return self._reconcile_suspend(defaulted)
        status = deep_copy(defaulted.get("status") or {})
        if is_condition_true(status, C.COND_SERVICE_SUSPENDED):
            status["conditions"] = [
                condition
                for condition in status.get("conditions") or []
                if condition.get("type") not in (C.COND_SERVICE_SUSPENDING, C.COND_SERVICE_SUSPENDED)
            ]
            status["serviceStatus"] = ""
            self._commit(defaulted, status)
            return True

        active_name = (status.get("activeServiceStatus") or {}).get("rayClusterName") or ""
        pending_name = (status.get("pendingServiceStatus") or {}).get("rayClusterName") or ""
        desired_hash = cluster_spec_hash(defaulted)
        strategy_type = ((defaulted["spec"].get("upgradeStrategy") or {}).get("type")) or "NewCluster"

        active = self._get_cluster(active_name, namespace) if active_name else None
        pending = self._get_cluster(pending_name, namespace) if pending_name else None
        active_revision = self._cluster_revision(active) if active else ""
        pending_revision = self._cluster_revision(pending) if pending else ""

        changed = False
        rollback_active = False
        # If the active cluster disappeared during an upgrade, adopt the
        # pending cluster instead of trying to create a second one with the
        # same revision name (which would fail with AlreadyExists forever).
        if active is None and pending is not None:
            active = pending
            active_name = pending_name
            active_revision = pending_revision
            status["activeServiceStatus"] = deep_copy(status.get("pendingServiceStatus") or {})
            status["pendingServiceStatus"] = {}
            pending = None
            pending_name = ""
            changed = True
        # Roll back an in-flight upgrade when the user reverts the spec.
        if pending and desired_hash == active_revision and pending_revision != active_revision:
            self._delete_cluster(pending)
            status["pendingServiceStatus"] = {}
            status["conditions"] = self._set_condition_list(defaulted, status, C.COND_ROLLBACK_IN_PROGRESS, True, "RollbackInProgress", "spec reverted")
            self._set_condition_list(defaulted, status, C.COND_UPGRADE_IN_PROGRESS, False, "NoPendingCluster", "")
            rollback_active = True
            changed = True
            pending = None
            pending_name = ""

        if active is None:
            active = self._create_cluster(defaulted, desired_hash)
            if active is None:
                return False
            active_name = active["metadata"]["name"]
            status["activeServiceStatus"] = {
                **(status.get("activeServiceStatus") or {}),
                "rayClusterName": active_name,
                "rayClusterStatus": deep_copy(active.get("status") or {}),
            }
            changed = True
        elif pending is None and active_revision and active_revision != desired_hash and strategy_type != "None":
            pending = self._create_cluster(defaulted, desired_hash)
            if pending is not None:
                pending_name = pending["metadata"]["name"]
                status["pendingServiceStatus"] = {
                    "rayClusterName": pending_name,
                    "rayClusterStatus": deep_copy(pending.get("status") or {}),
                    "trafficRoutedPercent": 0,
                    "targetCapacity": 0,
                    "applicationStatuses": {},
                }
                self._set_condition_list(defaulted, status, C.COND_UPGRADE_IN_PROGRESS, True, "BothActivePendingClustersExist", "upgrading")
                changed = True

        if pending is not None and strategy_type == "None":
            self._delete_cluster(pending)
            status["pendingServiceStatus"] = {}
            pending = None
            pending_name = ""
            changed = True

        if active:
            self._ensure_services(defaulted, active)
        if pending is not None:
            self._ensure_services(defaulted, pending, stable=False)
        if active and pending is None:
            changed |= self._patch_active_cluster(defaulted, active)

        # Deploy Serve on active and pending clusters.  Only the active cluster
        # receives traffic; the pending cluster is warmed up in isolation.
        # During an upgrade the active cluster must keep its OLD configuration:
        # applying the new serveConfigV2 to it would partially deploy the new
        # application and defeat the zero-downtime guarantee.
        if active and pending is None:
            active_ready, active_apps = self._reconcile_serve(defaulted, active)
        elif active:
            ready, statuses = self.runtime.serve_status(namespace, active["metadata"]["name"])
            active_ready = active.get("status", {}).get("state") == "ready" and ready
            active_apps = statuses
        else:
            active_ready, active_apps = False, {}
        # A pending cluster starts at 0% target capacity; traffic migration
        # raises it up to maxSurgePercent as replicas warm up.
        pending_ready, pending_apps = self._reconcile_serve(defaulted, pending, target_capacity=0) if pending else (False, {})

        if active:
            self._update_serve_labels(active, ready=active_ready, receive_traffic=True)
            pending_share = int((status.get("pendingServiceStatus") or {}).get("trafficRoutedPercent", 0) or 0)
            status["activeServiceStatus"] = {
                **(status.get("activeServiceStatus") or {}),
                "rayClusterName": active["metadata"]["name"],
                "rayClusterStatus": deep_copy(active.get("status") or {}),
                "applicationStatuses": active_apps,
                "targetCapacity": 100,
                "trafficRoutedPercent": max(0, 100 - pending_share),
            }
        if pending:
            # Incremental upgrades route a share of traffic to the pending
            # cluster before promotion, so its Serve Service must have
            # endpoints.  NewCluster keeps the pending cluster isolated until
            # the atomic switch.
            incremental = strategy_type == "NewClusterWithIncrementalUpgrade"
            self._update_serve_labels(
                pending,
                ready=pending_ready if incremental else False,
                receive_traffic=incremental,
            )
            status["pendingServiceStatus"] = {
                **(status.get("pendingServiceStatus") or {}),
                "rayClusterName": pending["metadata"]["name"],
                "rayClusterStatus": deep_copy(pending.get("status") or {}),
                "applicationStatuses": pending_apps,
                "targetCapacity": int((status.get("pendingServiceStatus") or {}).get("targetCapacity", 0) or 0),
                "trafficRoutedPercent": int((status.get("pendingServiceStatus") or {}).get("trafficRoutedPercent", 0)),
            }

        # Traffic switch / incremental migration.  Promotion writes a complete
        # new status and returns; the next reconcile validates the new active
        # cluster instead of mixing old and new snapshots.
        if pending is not None and pending_ready and all_apps_ready(pending_apps):
            strategy = defaulted["spec"].get("upgradeStrategy") or {}
            if strategy.get("type") == "NewClusterWithIncrementalUpgrade":
                percent = self._advance_traffic(defaulted, status, pending)
                if active:
                    status["activeServiceStatus"]["trafficRoutedPercent"] = max(0, 100 - percent)
                if percent >= 100:
                    self._promote(defaulted, status, active, pending)
                    return True
            else:
                self._promote(defaulted, status, active, pending)
                return True

        routed_percent = int((status.get("pendingServiceStatus") or {}).get("trafficRoutedPercent", 0) or 0)
        changed |= self._ensure_upgrade_routing(defaulted, active, pending, routed_percent)

        # Delete clusters whose post-switch delay has elapsed.
        changed |= self._reap_old_clusters(defaulted)

        endpoints = self._serve_endpoints(defaulted, status)
        ready = active_ready and all_apps_ready(active_apps) and endpoints > 0
        status["numServeEndpoints"] = endpoints
        status["serviceStatus"] = "Running" if ready else ""
        self._set_condition_list(
            defaulted,
            status,
            C.COND_READY,
            ready,
            "NonZeroServeEndpoints" if ready else "ZeroServeEndpoints",
            "" if ready else "RayService is not ready to serve requests",
        )
        if not rollback_active:
            self._set_condition_list(defaulted, status, C.COND_ROLLBACK_IN_PROGRESS, False, "NoPendingCluster", "")
        status["observedGeneration"] = defaulted["metadata"].get("generation", 1)
        changed |= self._commit(defaulted, status)
        self.metrics.service_ready(namespace, name, ready)
        return changed

    # ------------------------------------------------------------------ cleanup
    def _cleanup(self, service: dict) -> bool:
        namespace = obj_namespace(service)
        name = obj_name(service)
        clusters = self._owned_clusters(service)
        for cluster in clusters:
            self._delete_cluster(cluster)
        for kind in (C.KIND_SERVICE, C.KIND_ENDPOINTSLICE):
            for item in self.store.list(kind, namespace, label_selector={C.LABEL_ORIGINATED_NAME: name, C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE}):
                try:
                    self.store.delete(kind, item["metadata"]["name"], namespace, force=True)
                except Exception:
                    pass
        self._delete_routing(service)
        if clusters:
            return True
        if remove_finalizer(service, C.FINALIZER_RAYSERVICE):
            self.store.update(C.KIND_RAYSERVICE, service)
            return True
        return False

    def _reconcile_suspend(self, service: dict) -> bool:
        namespace = obj_namespace(service)
        name = obj_name(service)
        try:
            current = self.store.get(C.KIND_RAYSERVICE, name, namespace)
        except Exception:
            return False
        status = deep_copy(current.get("status") or {})
        clusters = self._owned_clusters(service)
        for cluster in clusters:
            self._delete_cluster(cluster)
        for kind in (C.KIND_SERVICE, C.KIND_ENDPOINTSLICE):
            for item in self.store.list(kind, namespace, label_selector={C.LABEL_ORIGINATED_NAME: name, C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE}):
                try:
                    self.store.delete(kind, item["metadata"]["name"], namespace, force=True)
                except Exception:
                    pass
        self._delete_routing(service)
        status["activeServiceStatus"] = {}
        status["pendingServiceStatus"] = {}
        status["numServeEndpoints"] = 0
        status["serviceStatus"] = ""
        proxy = {"metadata": current["metadata"], "status": status}
        set_condition(proxy, C.COND_READY, False, "Suspended", "RayService is suspended", clock=self.clock)
        set_condition(proxy, C.COND_UPGRADE_IN_PROGRESS, False, "Suspended", "", clock=self.clock)
        set_condition(proxy, C.COND_ROLLBACK_IN_PROGRESS, False, "Suspended", "", clock=self.clock)
        if clusters:
            set_condition(proxy, C.COND_SERVICE_SUSPENDING, True, "SuspendRequested", "deleting owned resources", clock=self.clock)
            set_condition(proxy, C.COND_SERVICE_SUSPENDED, False, "SuspendInProgress", "waiting", clock=self.clock)
        else:
            set_condition(proxy, C.COND_SERVICE_SUSPENDING, False, "SuspendComplete", "all resources deleted", clock=self.clock)
            set_condition(proxy, C.COND_SERVICE_SUSPENDED, True, "SuspendComplete", "RayService is suspended", clock=self.clock)
        return self._commit(current, proxy["status"])

    def _validation_failed(self, service: dict, message: str) -> bool:
        existing = service.get("status") or {}
        condition = next((item for item in existing.get("conditions") or [] if item.get("type") == C.COND_READY), None)
        if condition and condition.get("reason") == "ValidationFailed" and condition.get("message") == message:
            return False
        self.recorder.warning(service, "InvalidRayServiceSpec", message)
        status = deep_copy(existing)
        status["serviceStatus"] = ""
        proxy = {"metadata": service["metadata"], "status": status}
        set_condition(proxy, C.COND_READY, False, "ValidationFailed", message, clock=self.clock)
        return self._commit(service, proxy["status"])

    # ------------------------------------------------------------------ clusters
    def _owned_clusters(self, service: dict) -> list[dict]:
        return self.store.list(
            C.KIND_RAYCLUSTER,
            obj_namespace(service),
            label_selector={C.LABEL_ORIGINATED_NAME: obj_name(service), C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE},
        )

    def _get_cluster(self, name: str, namespace: str) -> dict | None:
        if not name:
            return None
        try:
            return self.store.get(C.KIND_RAYCLUSTER, name, namespace)
        except Exception:
            return None

    @staticmethod
    def _cluster_revision(cluster: dict | None) -> str:
        if not cluster:
            return ""
        return (cluster.get("metadata", {}).get("labels") or {}).get("minikuberay.io/service-revision", "")

    def _create_cluster(self, service: dict, revision: str) -> dict | None:
        from ..models.meta import new_object

        namespace = obj_namespace(service)
        name = cluster_name_for_service(obj_name(service), revision[:5])
        spec = deep_copy((service.get("spec") or {}).get("rayClusterConfig") or {})
        # IPPR lives on the RayCluster annotation.  A RayService only carries
        # a RayClusterSpec, so forward the annotation explicitly; otherwise
        # the child cluster silently ignores the autoscaler's vertical limits.
        annotations = {}
        parent_annotations = (service.get("metadata") or {}).get("annotations") or {}
        if C.ANNOTATION_IPPR in parent_annotations:
            annotations[C.ANNOTATION_IPPR] = parent_annotations[C.ANNOTATION_IPPR]
        cluster = new_object(
            C.RAY_API_VERSION,
            C.KIND_RAYCLUSTER,
            name,
            namespace,
            spec=spec,
            annotations=annotations,
            labels={
                C.LABEL_ORIGINATED_NAME: obj_name(service),
                C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE,
                "minikuberay.io/service-revision": revision,
                C.LABEL_KUBERAY_VERSION: "v1.7.1",
            },
            owner_references=[owner_reference(service)],
        )
        try:
            return self.store.create(C.KIND_RAYCLUSTER, cluster, namespace)
        except Exception as exc:
            self.recorder.warning(service, "FailedToCreateRayCluster", str(exc))
            return None

    def _patch_active_cluster(self, service: dict, cluster: dict) -> bool:
        """Apply non-rollout changes (worker replicas, serve-only changes).

        A change that requires new Pods has a different revision hash and goes
        through the pending-cluster path.  Changes excluded from the hash (for
        example ``replicas``) must be patched into the active RayCluster in
        place, otherwise ``upgradeStrategy: None`` or a pure replica edit would
        silently do nothing.
        """
        if cluster_spec_hash(service) != self._cluster_revision(cluster):
            return False
        desired = deep_copy((service.get("spec") or {}).get("rayClusterConfig") or {})
        if not desired:
            return False
        marker = stable_hash(desired, 12)
        annotations = (service.get("metadata") or {}).get("annotations") or {}
        if annotations.get(C.ANNOTATION_APPLIED_CLUSTER_CONFIG) == marker:
            # The service spec has not changed since the last patch.  Do not
            # fight the autoscaler, which may have changed replicas on the
            # RayCluster since then.
            return False
        try:
            current = self.store.get(C.KIND_RAYCLUSTER, cluster["metadata"]["name"], cluster["metadata"]["namespace"])
        except Exception:
            return False
        if canonical_json(current.get("spec") or {}) != canonical_json(desired):
            current["spec"] = desired
            self.store.update(C.KIND_RAYCLUSTER, current)
        try:
            service_obj = self.store.get(C.KIND_RAYSERVICE, obj_name(service), obj_namespace(service))
            service_obj.setdefault("metadata", {}).setdefault("annotations", {})[C.ANNOTATION_APPLIED_CLUSTER_CONFIG] = marker
            self.store.update(C.KIND_RAYSERVICE, service_obj)
        except Exception:
            pass
        return True

    def _delete_cluster(self, cluster: dict) -> None:
        try:
            self.store.delete(C.KIND_RAYCLUSTER, cluster["metadata"]["name"], cluster["metadata"]["namespace"])
        except Exception:
            pass

    # --------------------------------------------------------------------- serve
    def _reconcile_serve(self, service: dict, cluster: dict, *, target_capacity: int = 100) -> tuple[bool, dict]:
        namespace = obj_namespace(service)
        config = (service.get("spec") or {}).get("serveConfigV2", "")
        annotations = (service.get("metadata") or {}).get("annotations") or {}
        deploy_seconds = 0.0
        if C.ANNOTATION_SIM_DEPLOY_DELAY in annotations:
            deploy_seconds = float(annotations[C.ANNOTATION_SIM_DEPLOY_DELAY])
        self.runtime.deploy_serve(namespace, cluster["metadata"]["name"], config, deploy_seconds=deploy_seconds, target_capacity=target_capacity)
        self.runtime.set_target_capacity(namespace, cluster["metadata"]["name"], target_capacity)
        ready, statuses = self.runtime.serve_status(namespace, cluster["metadata"]["name"])
        cluster_ready = cluster.get("status", {}).get("state") == "ready"
        return cluster_ready and ready, statuses if ready else statuses_for_apps(config, ready=False, message="deploying")

    def _update_serve_labels(self, cluster: dict, *, ready: bool, receive_traffic: bool) -> None:
        namespace = obj_namespace(cluster)
        exclude_head = False
        try:
            service = self.store.get(C.KIND_RAYSERVICE, (cluster["metadata"].get("labels") or {}).get(C.LABEL_ORIGINATED_NAME, ""), namespace)
            exclude_head = bool((service.get("spec") or {}).get("excludeHeadPodFromServeSvc"))
        except Exception:
            pass
        for pod in self.store.list(C.KIND_POD, namespace, label_selector={C.LABEL_CLUSTER: cluster["metadata"]["name"]}):
            node_type = (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE)
            enabled = ready and receive_traffic
            if node_type == C.NODE_TYPE_HEAD and exclude_head:
                enabled = False
            labels = pod["metadata"].setdefault("labels", {})
            if str(labels.get(C.LABEL_SERVE, "")).lower() != str(enabled).lower():
                labels[C.LABEL_SERVE] = "true" if enabled else "false"
                try:
                    self.store.update(C.KIND_POD, pod)
                except Exception:
                    pass

    def _serve_endpoints(self, service: dict, status: dict) -> int:
        namespace = obj_namespace(service)
        active_name = (status.get("activeServiceStatus") or {}).get("rayClusterName")
        if not active_name:
            return 0
        pods = self.store.list(
            C.KIND_POD,
            namespace,
            label_selector={C.LABEL_CLUSTER: active_name, C.LABEL_SERVE: "true"},
        )
        return sum(1 for pod in pods if pod_is_ready(pod))

    # ------------------------------------------------------------------ upgrade
    def _advance_traffic(self, service: dict, status: dict, pending: dict) -> int:
        options = ((service.get("spec") or {}).get("upgradeStrategy") or {}).get("clusterUpgradeOptions") or {}
        step = int(options.get("stepSizePercent", 10))
        interval = float(options.get("intervalSeconds", 10))
        pending_status = status.get("pendingServiceStatus") or {}
        current = int(pending_status.get("trafficRoutedPercent", 0))
        last = pending_status.get("lastTrafficMigratedTime") or pending_status.get("_lastMigration")
        elapsed = interval
        if last:
            try:
                elapsed = (self.clock.now() - parse_iso(last)).total_seconds()
            except Exception:
                elapsed = interval
        if current < 100 and elapsed >= interval:
            current = min(100, current + step)
            pending_status["trafficRoutedPercent"] = current
            pending_status["lastTrafficMigratedTime"] = iso(self.clock.now())
        max_surge = int(options.get("maxSurgePercent", 100))
        # warm-up capacity follows the traffic share, but never exceeds the
        # configured surge budget.
        target_capacity = min(max_surge, max(step, current))
        pending_status["targetCapacity"] = target_capacity
        status["pendingServiceStatus"] = pending_status
        namespace = obj_namespace(service)
        self.runtime.set_serve_traffic(namespace, pending["metadata"]["name"], current)
        self.runtime.set_target_capacity(namespace, pending["metadata"]["name"], target_capacity)
        return current

    def _promote(self, service: dict, status: dict, active: dict | None, pending: dict) -> bool:
        namespace = obj_namespace(service)
        pending_status = deep_copy(status.get("pendingServiceStatus") or {})
        pending_status["trafficRoutedPercent"] = 100
        pending_status["targetCapacity"] = 100
        old_active = active
        status["activeServiceStatus"] = pending_status
        status["pendingServiceStatus"] = {}
        status["numServeEndpoints"] = 0
        status["conditions"] = [
            condition
            for condition in status.get("conditions") or []
            if condition.get("type") not in (C.COND_UPGRADE_IN_PROGRESS, C.COND_ROLLBACK_IN_PROGRESS)
        ]
        if old_active and old_active["metadata"]["name"] != pending["metadata"]["name"]:
            annotations = old_active["metadata"].setdefault("annotations", {})
            delay = int((service.get("spec") or {}).get("rayClusterDeletionDelaySeconds", C.DEFAULT_RAYCLUSTER_DELETION_DELAY_SECONDS))
            annotations["minikuberay.io/delete-after"] = iso(self.clock.now() + timedelta(seconds=delay))
            try:
                self.store.update(C.KIND_RAYCLUSTER, old_active)
            except Exception:
                pass
        self._update_serve_labels(pending, ready=True, receive_traffic=True)
        self._ensure_services(service, pending)
        # The promoted cluster is now the only backend; keeping a 0-weight
        # reference to the deleted cluster would make the Gateway drop requests.
        self._ensure_upgrade_routing(service, pending, None, 0)
        return self._commit(service, status)

    def _reap_old_clusters(self, service: dict) -> bool:
        changed = False
        active_name = ((self.store.get(C.KIND_RAYSERVICE, obj_name(service), obj_namespace(service)).get("status") or {}).get("activeServiceStatus") or {}).get("rayClusterName")
        for cluster in self._owned_clusters(service):
            if cluster["metadata"]["name"] == active_name:
                continue
            delete_after = (cluster["metadata"].get("annotations") or {}).get("minikuberay.io/delete-after")
            if not delete_after:
                continue
            try:
                if self.clock.now() >= parse_iso(delete_after):
                    self._delete_cluster(cluster)
                    changed = True
            except Exception:
                pass
        return changed

    # ------------------------------------------------------------------ services
    def _ensure_services(self, service: dict, cluster: dict, *, stable: bool = True) -> None:
        """Create the stable Services and (for incremental upgrades) per-cluster ones.

        ``stable=True`` owns the client-facing ``<service>-serve-svc`` and
        ``<service>-head-svc``.  A pending cluster only gets its own
        ``<cluster>-serve-svc`` so the HTTPRoute can send it a weighted share
        before it is promoted.
        """
        namespace = obj_namespace(service)
        name = obj_name(service)
        labels = {
            C.LABEL_ORIGINATED_NAME: name,
            C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE,
        }
        cluster_name = cluster["metadata"]["name"]
        if stable:
            head_service = new_service(
                f"{name}-head-svc",
                namespace,
                {C.LABEL_CLUSTER: cluster_name, C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD},
                head_service_ports(),
                cluster_ip="None",
                publish_not_ready=True,
                labels={**labels, C.LABEL_NODE_TYPE: C.NODE_TYPE_HEAD},
                owner_references=[owner_reference(service)],
            )
            self.ensure(C.KIND_SERVICE, head_service)
            custom = (service.get("spec") or {}).get("serveService") or {}
            custom_name = (custom.get("metadata") or {}).get("name") or f"{name}-serve-svc"
            serve_service = new_service(
                custom_name,
                namespace,
                {C.LABEL_CLUSTER: cluster_name, C.LABEL_SERVE: "true"},
                [service_port("serve", C.PORT_SERVE)],
                labels=labels,
                owner_references=[owner_reference(service)],
            )
            if custom:
                custom_spec = custom.get("spec") or {}
                if custom_spec.get("ports"):
                    serve_service["spec"]["ports"] = deep_copy(custom_spec["ports"])
                if custom_spec.get("type"):
                    serve_service["spec"]["type"] = custom_spec["type"]
                serve_service["metadata"]["annotations"] = deep_copy(custom.get("metadata", {}).get("annotations") or {})
            self.ensure(C.KIND_SERVICE, serve_service)

        if self._strategy_type(service) == "NewClusterWithIncrementalUpgrade":
            cluster_service = new_service(
                f"{cluster_name}-serve-svc",
                namespace,
                {C.LABEL_CLUSTER: cluster_name, C.LABEL_SERVE: "true"},
                [service_port("serve", C.PORT_SERVE)],
                labels={**labels, C.LABEL_CLUSTER: cluster_name},
                owner_references=[owner_reference(service)],
            )
            self.ensure(C.KIND_SERVICE, cluster_service)

        # Switching back from incremental to NewCluster must not leave the
        # per-cluster weighted Services orphaned.
        if self._strategy_type(service) != "NewClusterWithIncrementalUpgrade":
            self._prune_cluster_services(service)

    def _prune_cluster_services(self, service: dict) -> bool:
        namespace = obj_namespace(service)
        name = obj_name(service)
        custom = (service.get("spec") or {}).get("serveService") or {}
        stable_name = (custom.get("metadata") or {}).get("name") or f"{name}-serve-svc"
        selector = {
            C.LABEL_ORIGINATED_NAME: name,
            C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE,
        }
        changed = False
        for item in self.store.list(C.KIND_SERVICE, namespace, label_selector=selector):
            service_name = item["metadata"]["name"]
            if service_name in (stable_name, f"{name}-head-svc"):
                continue
            if service_name.endswith("-serve-svc"):
                try:
                    self.store.delete(C.KIND_SERVICE, service_name, namespace, force=True)
                    changed = True
                except Exception:
                    pass
        return changed

    # ------------------------------------------------------------------- routing
    @staticmethod
    def _strategy_type(service: dict) -> str:
        return (((service.get("spec") or {}).get("upgradeStrategy") or {}).get("type")) or "NewCluster"

    def _delete_routing(self, service: dict) -> bool:
        """Remove Gateway/HTTPRoute objects owned by this RayService."""
        namespace = obj_namespace(service)
        selector = {
            C.LABEL_ORIGINATED_NAME: obj_name(service),
            C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE,
        }
        changed = False
        for kind in (C.KIND_HTTPROUTE, C.KIND_GATEWAY):
            for item in self.store.list(kind, namespace, label_selector=selector):
                try:
                    self.store.delete(kind, item["metadata"]["name"], namespace, force=True)
                    changed = True
                except Exception:
                    pass
        return changed

    def _ensure_upgrade_routing(self, service: dict, active: dict | None, pending: dict | None, percent: int) -> bool:
        """Maintain the weighted Gateway API objects for incremental upgrades.

        Non-incremental strategies must not leave a stale HTTPRoute behind when
        a user switches strategies, so they actively prune owned routing objects.
        """
        if self._strategy_type(service) != "NewClusterWithIncrementalUpgrade":
            return self._delete_routing(service)
        options = ((service.get("spec") or {}).get("upgradeStrategy") or {}).get("clusterUpgradeOptions") or {}
        namespace = obj_namespace(service)
        name = obj_name(service)
        labels = {
            C.LABEL_ORIGINATED_NAME: name,
            C.LABEL_ORIGINATED_CRD: C.KIND_RAYSERVICE,
        }
        percent = max(0, min(100, int(percent)))
        refs: list[dict] = []
        if active is not None:
            refs.append(
                {
                    "group": "",
                    "kind": "Service",
                    "name": f"{active['metadata']['name']}-serve-svc",
                    "namespace": namespace,
                    "port": C.PORT_SERVE,
                    "weight": 100 - percent,
                }
            )
        if pending is not None:
            refs.append(
                {
                    "group": "",
                    "kind": "Service",
                    "name": f"{pending['metadata']['name']}-serve-svc",
                    "namespace": namespace,
                    "port": C.PORT_SERVE,
                    "weight": percent,
                }
            )
        if not refs:
            return False

        # Adopt-an-existing-HTTPRoute mode: patch only the weighted backends so
        # the route can stay under GitOps ownership.
        adopted = options.get("httpRouteName")
        if adopted:
            try:
                route = self.store.get(C.KIND_HTTPROUTE, adopted, namespace)
            except Exception:
                return False
            changed = False
            rules = (route.get("spec") or {}).get("rules") or [{}]
            if canonical_json(rules[0].get("backendRefs") or []) != canonical_json(refs):
                rules[0]["backendRefs"] = deep_copy(refs)
                route.setdefault("spec", {})["rules"] = rules
                changed = True
            # While an upgrade is in progress, projects may need to tell
            # GitOps to ignore the temporarily patched route.
            in_progress = options.get("httpRouteInProgressLabels") or {}
            labels = route.setdefault("metadata", {}).setdefault("labels", {})
            for key, value in (in_progress.items() if pending is not None else ()):
                if labels.get(key) != value:
                    labels[key] = value
                    changed = True
            if pending is None:
                for key in in_progress:
                    if key in labels:
                        labels.pop(key)
                        changed = True
            if not changed:
                return False
            self.store.update(C.KIND_HTTPROUTE, route)
            return True

        if not options.get("gatewayClassName"):
            return False
        gateway_name = options.get("gatewayName") or f"{name}-gateway"
        gateway = build_gateway(
            name,
            namespace,
            options["gatewayClassName"],
            name=gateway_name,
            labels=labels,
            owner_references=[owner_reference(service)],
        )
        changed = self.ensure(C.KIND_GATEWAY, gateway)[1]

        route_name = options.get("httpRouteName") or f"{name}-httproute"
        route = build_httproute(
            name,
            namespace,
            refs,
            gateway_name=gateway_name,
            name=route_name,
            labels=labels,
            owner_references=[owner_reference(service)],
        )
        changed |= self.ensure(C.KIND_HTTPROUTE, route)[1]
        return changed

    # -------------------------------------------------------------------- status
    def _set_condition_list(self, service: dict, status: dict, condition_type: str, condition_status: bool, reason: str, message: str) -> list[dict]:
        proxy = {"metadata": {"generation": service.get("metadata", {}).get("generation", 1)}, "status": status}
        set_condition(proxy, condition_type, condition_status, reason, message, clock=self.clock)
        return proxy["status"].get("conditions", [])

    def _commit(self, service: dict, status: dict) -> bool:
        namespace = obj_namespace(service)
        name = obj_name(service)
        try:
            current = self.store.get(C.KIND_RAYSERVICE, name, namespace)
        except Exception:
            return False
        status = deep_copy(status)
        status["observedGeneration"] = current["metadata"].get("generation", 1)
        old = deep_copy(current.get("status") or {})
        old_cmp = {key: value for key, value in old.items() if key != "lastUpdateTime"}
        new_cmp = {key: value for key, value in status.items() if key != "lastUpdateTime"}
        if canonical_json(old_cmp) == canonical_json(new_cmp):
            return False
        status["lastUpdateTime"] = iso(self.clock.now())
        updated = deep_copy(current)
        updated["status"] = status
        self.store.update_status(updated)
        return True
