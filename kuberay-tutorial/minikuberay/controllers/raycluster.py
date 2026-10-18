"""RayCluster reconciler: the heart of the operator."""

from __future__ import annotations

from .. import constants as C
from ..autoscaler.policy import AutoscalerPolicy
from ..models.core import pod_is_ready, pod_phase, pod_requested_resources
from ..models.defaults import apply_defaults
from ..models.ippr import apply_resize, parse_ippr, rollback, status_of
from ..models.meta import (
    add_finalizer,
    deletion_timestamp,
    has_finalizer,
    is_condition_true,
    name as obj_name,
    namespace as obj_namespace,
    owner_reference,
    remove_finalizer,
    set_condition,
)
from ..models.raycluster import (
    desired_worker_replicas,
    group_desired_pods,
    group_replicas,
    head_service_name,
    max_worker_replicas,
    min_worker_replicas,
    pod_labels,
    worker_group_capacity,
)
from ..models.validation import validate_object
from ..util import (
    canonical_json,
    deep_copy,
    format_cpu,
    format_memory,
    parse_duration_seconds,
    resource_quantity,
    stable_hash,
    status_quantity,
)
from .base import Controller
from .builders import (
    build_auth_secret,
    build_autoscaler_rbac,
    build_gcs_pvc,
    build_head_pod,
    build_head_service,
    build_headless_service,
    build_ingress,
    build_network_policies,
    build_podgroup,
    build_redis_cleanup_job,
    build_serve_service,
    build_worker_pod,
    scheduler_name_for,
)


class RayClusterController(Controller):
    name = "raycluster"
    kind = C.KIND_RAYCLUSTER

    # ---------------------------------------------------------------- lifecycle
    def reconcile(self, obj: dict) -> bool:
        cluster = deep_copy(obj)
        namespace = obj_namespace(cluster)
        cluster_name = obj_name(cluster)

        if deletion_timestamp(cluster):
            return self._cleanup(cluster)

        # Objects handed to an external controller are not ours to touch; do
        # not even add a finalizer (otherwise deletion would stall on KubeRay).
        if (cluster.get("spec") or {}).get("managedBy") == C.MANAGED_BY_MULTIKUEUE:
            return False

        if add_finalizer(cluster, C.FINALIZER_RAYCLUSTER):
            cluster["metadata"]["resourceVersion"] = obj["metadata"]["resourceVersion"]
            self.store.update(C.KIND_RAYCLUSTER, cluster)
            return True

        # GCS FT with Redis needs an extra finalizer so the Redis namespace can
        # be flushed after the Pods stop and before the CR disappears.
        ft = (cluster.get("spec") or {}).get("gcsFaultToleranceOptions") or {}
        if ft and ft.get("backend", "redis") == "redis":
            if add_finalizer(cluster, C.FINALIZER_GCS_FT):
                cluster["metadata"]["resourceVersion"] = obj["metadata"]["resourceVersion"]
                self.store.update(C.KIND_RAYCLUSTER, cluster)
                return True
        elif has_finalizer(cluster, C.FINALIZER_GCS_FT):
            remove_finalizer(cluster, C.FINALIZER_GCS_FT)
            cluster["metadata"]["resourceVersion"] = obj["metadata"]["resourceVersion"]
            self.store.update(C.KIND_RAYCLUSTER, cluster)
            return True

        defaulted = apply_defaults(cluster)
        if canonical_json(defaulted.get("spec")) != canonical_json(cluster.get("spec")):
            merged = deep_copy(cluster)
            merged["spec"] = defaulted["spec"]
            merged["status"] = cluster.get("status", {})
            self.store.update(C.KIND_RAYCLUSTER, merged)
            return True

        # A failed in-place resize must be rolled back even if the annotation
        # was removed, autoscaling was disabled, or the new spec no longer
        # validates.  Otherwise the Pod keeps an inflated request that the
        # Raylet never saw, and a horizontal replica is added on top of it.
        recovered = self._recover_failed_resizes(defaulted)

        errors = validate_object(defaulted)
        if errors:
            invalid = self._invalid(defaulted, "; ".join(errors))
            return recovered or invalid

        if defaulted.get("spec", {}).get("managedBy") == C.MANAGED_BY_MULTIKUEUE:
            return False

        if defaulted.get("spec", {}).get("tlsOptions", {}).get("enabled") and not self.config.enabled("RayClusterMTLS"):
            return self._invalid(defaulted, "tlsOptions requires the RayClusterMTLS feature gate")
        if defaulted.get("spec", {}).get("networkPolicy") and not self.config.enabled("RayClusterNetworkPolicy"):
            return self._invalid(defaulted, "spec.networkPolicy requires the RayClusterNetworkPolicy feature gate")
        if defaulted.get("spec", {}).get("historyServerOptions") and not self.config.enabled("RayClusterHistoryServer"):
            return self._invalid(defaulted, "spec.historyServerOptions requires the RayClusterHistoryServer feature gate")
        if defaulted.get("spec", {}).get("gcsFaultToleranceOptions", {}).get("backend") == "rocksdb" and not self.config.enabled("GCSFaultToleranceEmbeddedStorage"):
            return self._invalid(defaulted, "the rocksdb GCS FT backend requires the GCSFaultToleranceEmbeddedStorage feature gate")
        if self.config.batch_scheduler and self.config.batch_scheduler not in C.BATCH_SCHEDULERS:
            return self._invalid(defaulted, f"unknown batch scheduler {self.config.batch_scheduler!r}")
        if self.config.batch_scheduler == "was" and not self.config.enabled("KubernetesWAS"):
            return self._invalid(defaulted, "the 'was' batch scheduler requires the KubernetesWAS feature gate")
        if not self.config.enabled("RayMultiHostIndexing") and any(
            int(group.get("numOfHosts", 1) or 1) > 1 for group in defaulted["spec"].get("workerGroupSpecs") or []
        ):
            return self._invalid(defaulted, "numOfHosts > 1 requires the RayMultiHostIndexing feature gate")

        if defaulted.get("spec", {}).get("suspend"):
            return self._reconcile_suspend(defaulted)
        self._clear_suspend_conditions(defaulted)

        # Recreate/None upgrade strategies are evaluated before child creation.
        upgrade = (defaulted.get("spec") or {}).get("upgradeStrategy") or {}
        strategy_type = upgrade.get("type")
        stored_hash = self._stored_hash(cluster)
        current_hash = self._spec_hash(defaulted)
        pod_hashes = {
            (pod["metadata"].get("labels") or {}).get(C.ANNOTATION_SPEC_HASH)
            or (pod["metadata"].get("labels") or {}).get("ray.io/spec-hash")
            for pod in self._all_pods(cluster)
        }
        pod_hashes.discard(None)
        stale_pods = bool(pod_hashes) and any(value != current_hash for value in pod_hashes)
        # ``None`` means "do not replace existing Pods".  It must NOT skip the
        # rest of reconcile: Services/RBAC/status still need repair, and missing
        # Pods (e.g. the head) still need to be recreated with the new template.
        if strategy_type == "Recreate" and self._all_pods(cluster) and (stale_pods or (stored_hash and stored_hash != current_hash)):
            self.recorder.normal(defaulted, "RecreateUpgrade", "spec changed; deleting pods for Recreate strategy")
            self._delete_all_pods(cluster)
            return True

        changed = False
        changed |= self._ensure_auxiliary(defaulted)
        changed |= self._ensure_services(defaulted)
        changed |= self._prune_auxiliary(defaulted)
        changed |= self._ensure_pods(defaulted, current_hash)
        changed |= self._autoscale(defaulted)
        changed |= self._write_status(defaulted)
        changed |= self._remember_hash(defaulted, current_hash)
        return changed

    def _invalid(self, cluster: dict, message: str) -> bool:
        namespace = obj_namespace(cluster)
        name = obj_name(cluster)
        try:
            current = self.store.get(C.KIND_RAYCLUSTER, name, namespace)
        except Exception:
            return False
        current_status = current.get("status") or {}
        if current_status.get("reason") == "ValidationFailed" and current_status.get("message") == message:
            # Idempotency: the same invalid spec must not emit a new Event on
            # every rescan.  Otherwise events (with random names) keep bumping
            # resourceVersion and `run_once()` never reports convergence.
            return False
        self.recorder.warning(current, "InvalidRayClusterSpec", message)
        status = deep_copy(current_status)
        status["reason"] = "ValidationFailed"
        status["message"] = message
        status["observedGeneration"] = cluster["metadata"].get("generation", 1)
        proxy = {"metadata": current["metadata"], "status": status}
        set_condition(proxy, C.COND_PROVISIONED, False, "ValidationFailed", message, clock=self.clock)
        status = proxy["status"]
        return self._commit_status(current, status)

    def _cleanup(self, cluster: dict) -> bool:
        namespace = obj_namespace(cluster)
        name = obj_name(cluster)
        if has_finalizer(cluster, C.FINALIZER_GCS_FT):
            return self._cleanup_gcs_ft(cluster)
        changed = False
        for pod in self._all_pods(cluster):
            try:
                self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                self.runtime.unregister_pod(namespace, name, pod["metadata"]["name"])
                changed = True
            except Exception:
                pass
        for kind in (C.KIND_SERVICE, C.KIND_ENDPOINTSLICE, C.KIND_NETWORKPOLICY, C.KIND_PVC, C.KIND_SECRET, C.KIND_SERVICEACCOUNT, C.KIND_ROLE, C.KIND_ROLEBINDING, C.KIND_PODGROUP, C.KIND_INGRESS, C.KIND_JOB):
            for item in self.store.list(kind, namespace, label_selector={C.LABEL_CLUSTER: name}):
                if not self._owned_by_cluster(item, cluster):
                    continue
                if kind == C.KIND_PVC and (item.get("metadata", {}).get("annotations") or {}).get("minikuberay.io/retain") == "true":
                    continue
                try:
                    self.store.delete(kind, item["metadata"]["name"], namespace, force=True)
                    changed = True
                except Exception:
                    pass
        self.runtime.remove_cluster(namespace, name)
        if remove_finalizer(cluster, C.FINALIZER_RAYCLUSTER):
            self.recorder.normal(cluster, "DeletedRayCluster", f"Deleted RayCluster {name} and its child resources")
            self.store.update(C.KIND_RAYCLUSTER, cluster)
            changed = True
        return changed

    def _cleanup_gcs_ft(self, cluster: dict) -> bool:
        """Two-phase GCS FT cleanup: stop Pods, flush Redis, then release the CR."""
        namespace = obj_namespace(cluster)
        name = obj_name(cluster)
        changed = False

        # Phase 1: stop all Ray node Pods so no writer touches Redis during
        # cleanup.  Do not touch the cleanup Job's own submitter Pod (it also
        # carries the cluster label).
        for pod in self._all_pods(cluster):
            if (pod["metadata"].get("labels") or {}).get(C.LABEL_IS_RAY_NODE) != "yes":
                continue
            try:
                self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                self.runtime.unregister_pod(namespace, name, pod["metadata"]["name"])
                changed = True
            except Exception:
                pass

        # Phase 2: run the Redis cleanup Job (simulated) and wait for completion.
        job_name = f"{name}-redis-cleanup"
        try:
            job = self.store.get(C.KIND_JOB, job_name, namespace)
        except Exception:
            try:
                job = build_redis_cleanup_job(cluster)
                self.store.create(C.KIND_JOB, job, namespace)
                self.recorder.normal(cluster, "CreatedGCSFTRedisCleanupJob", f"Created cleanup Job {job_name}")
                return True
            except Exception as exc:
                self.recorder.warning(cluster, "FailedToCreateGCSFTRedisCleanupJob", str(exc))
                return changed

        complete = any(
            condition.get("type") == "Complete" and condition.get("status") == "True"
            for condition in (job.get("status") or {}).get("conditions") or []
        )
        if complete:
            if remove_finalizer(cluster, C.FINALIZER_GCS_FT):
                self.recorder.normal(cluster, "GCSFTRedisCleanupComplete", f"Removed {C.FINALIZER_GCS_FT}")
                self.store.update(C.KIND_RAYCLUSTER, cluster)
                return True
            return changed

        annotations = (cluster.get("metadata") or {}).get("annotations") or {}
        raw_timeout = annotations.get(C.ANNOTATION_GCS_FT_DELETION_TIMEOUT, "300s")
        try:
            timeout = parse_duration_seconds(raw_timeout, 300.0)
        except ValueError:
            timeout = 300.0
        deletion = deletion_timestamp(cluster)
        if deletion:
            from ..clock import parse_iso

            elapsed = (self.clock.now() - parse_iso(deletion)).total_seconds()
            if elapsed >= timeout:
                if remove_finalizer(cluster, C.FINALIZER_GCS_FT):
                    self.recorder.warning(
                        cluster,
                        "GCSFTRedisCleanupTimeout",
                        f"cleanup exceeded {timeout:g}s; releasing the finalizer (Redis data may remain)",
                    )
                    self.store.update(C.KIND_RAYCLUSTER, cluster)
                    return True
        return changed

    # ------------------------------------------------------------------ suspend
    def _reconcile_suspend(self, cluster: dict) -> bool:
        namespace = obj_namespace(cluster)
        name = obj_name(cluster)
        try:
            current = self.store.get(C.KIND_RAYCLUSTER, name, namespace)
        except Exception:
            return False
        status = deep_copy(current.get("status") or {})
        pods = self._all_pods(cluster)
        changed = False
        proxy = {"metadata": current["metadata"], "status": status}
        if pods:
            set_condition(proxy, C.COND_SUSPENDING, True, "SuspendRequested", "deleting all Ray Pods", clock=self.clock)
            set_condition(proxy, C.COND_SUSPENDED, False, "SuspendInProgress", "waiting for Pods to terminate", clock=self.clock)
            status = proxy["status"]
            status["state"] = ""
            status["observedGeneration"] = cluster["metadata"].get("generation", 1)
            for pod in pods:
                try:
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                    self.runtime.unregister_pod(namespace, name, pod["metadata"]["name"])
                    changed = True
                except Exception:
                    pass
            changed |= self._commit_status(current, status)
            return True
        set_condition(proxy, C.COND_SUSPENDING, False, "SuspendComplete", "all Pods deleted", clock=self.clock)
        set_condition(proxy, C.COND_SUSPENDED, True, "SuspendComplete", "RayCluster is suspended", clock=self.clock)
        status = proxy["status"]
        status["state"] = "suspended"
        status["desiredWorkerReplicas"] = 0
        status["readyWorkerReplicas"] = 0
        status["availableWorkerReplicas"] = 0
        status["observedGeneration"] = cluster["metadata"].get("generation", 1)
        return self._commit_status(current, status) or changed

    @staticmethod
    def _clear_suspend_conditions(cluster: dict) -> None:
        status = cluster.setdefault("status", {})
        for condition in status.get("conditions") or []:
            if condition.get("type") in (C.COND_SUSPENDING, C.COND_SUSPENDED):
                condition["status"] = "False"
                condition["reason"] = "RayClusterResumed"

    # ----------------------------------------------------------------- auxiliary
    def _ensure_auxiliary(self, cluster: dict) -> bool:
        changed = False
        spec = cluster.get("spec") or {}
        if spec.get("enableInTreeAutoscaling"):
            for obj in build_autoscaler_rbac(cluster):
                changed |= self.ensure(obj["kind"], obj)[1]
        pvc = build_gcs_pvc(cluster)
        if pvc:
            changed |= self.ensure(C.KIND_PVC, pvc)[1]
        secret = build_auth_secret(cluster)
        if secret:
            changed |= self.ensure(C.KIND_SECRET, secret)[1]
        if (spec.get("tlsOptions") or {}).get("enabled"):
            from .builders import build_tls_secret

            changed |= self.ensure(C.KIND_SECRET, build_tls_secret(cluster, C.NODE_TYPE_HEAD))[1]
            changed |= self.ensure(C.KIND_SECRET, build_tls_secret(cluster, C.NODE_TYPE_WORKER))[1]
        for policy in build_network_policies(cluster):
            changed |= self.ensure(C.KIND_NETWORKPOLICY, policy)[1]
        podgroup = build_podgroup(cluster, self.config.batch_scheduler)
        if podgroup:
            changed |= self.ensure(C.KIND_PODGROUP, podgroup)[1]
        ingress = build_ingress(cluster)
        if ingress:
            changed |= self.ensure(C.KIND_INGRESS, ingress)[1]
        return changed

    def _ensure_services(self, cluster: dict) -> bool:
        changed = False
        desired = build_head_service(cluster)
        current = None
        try:
            current = self.store.get(C.KIND_SERVICE, desired["metadata"]["name"], obj_namespace(cluster))
        except Exception:
            pass
        if current:
            desired["spec"]["clusterIP"] = current.get("spec", {}).get("clusterIP") or "None"
            desired["spec"]["clusterIPs"] = current.get("spec", {}).get("clusterIPs") or [desired["spec"]["clusterIP"]]
        changed |= self.ensure(C.KIND_SERVICE, desired)[1]

        if (cluster.get("metadata", {}).get("annotations") or {}).get(C.ANNOTATION_ENABLE_SERVE_SERVICE) == "true":
            changed |= self.ensure(C.KIND_SERVICE, build_serve_service(cluster))[1]
        if any(int(group.get("numOfHosts", 1) or 1) > 1 for group in (cluster.get("spec") or {}).get("workerGroupSpecs") or []):
            changed |= self.ensure(C.KIND_SERVICE, build_headless_service(cluster))[1]
        return changed

    def _prune_auxiliary(self, cluster: dict) -> bool:
        """Remove optional children when their feature is turned off."""
        spec = cluster.get("spec") or {}
        namespace = obj_namespace(cluster)
        name = obj_name(cluster)
        desired: dict[str, set[str]] = {
            C.KIND_SERVICE: {head_service_name(cluster)},
            C.KIND_NETWORKPOLICY: set(),
            C.KIND_SERVICEACCOUNT: set(),
            C.KIND_ROLE: set(),
            C.KIND_ROLEBINDING: set(),
            C.KIND_SECRET: set(),
            C.KIND_PVC: set(),
            C.KIND_PODGROUP: set(),
            C.KIND_INGRESS: set(),
        }
        if self.config.batch_scheduler in ("volcano", "scheduler-plugins"):
            desired[C.KIND_PODGROUP].add(f"{name}-podgroup")
        if (spec.get("headGroupSpec") or {}).get("enableIngress"):
            desired[C.KIND_INGRESS].add(f"{name}-ingress")
        if any(int(group.get("numOfHosts", 1) or 1) > 1 for group in spec.get("workerGroupSpecs") or []):
            desired[C.KIND_SERVICE].add(f"{name}-headless")
        if (cluster.get("metadata", {}).get("annotations") or {}).get(C.ANNOTATION_ENABLE_SERVE_SERVICE) == "true":
            desired[C.KIND_SERVICE].add(f"{name}-serve-svc")
        if spec.get("networkPolicy"):
            desired[C.KIND_NETWORKPOLICY].update({f"{name}-head", f"{name}-worker"})
        if spec.get("enableInTreeAutoscaling"):
            desired[C.KIND_SERVICEACCOUNT].add(f"{name}-autoscaler")
            desired[C.KIND_ROLE].add(f"{name}-autoscaler")
            desired[C.KIND_ROLEBINDING].add(f"{name}-autoscaler")
        auth = spec.get("authOptions") or {}
        if auth and auth.get("mode", "token") == "token" and not auth.get("secretName"):
            desired[C.KIND_SECRET].add(f"{name}-auth")
        if (spec.get("tlsOptions") or {}).get("enabled"):
            desired[C.KIND_SECRET].update({f"{name}-head-tls", f"{name}-worker-tls"})
        ft = spec.get("gcsFaultToleranceOptions") or {}
        if ft.get("backend") == "rocksdb":
            storage = ft.get("storage") or {}
            claim_name = storage.get("claimName")
            if not claim_name or claim_name == f"{name}-gcs-pvc":
                desired[C.KIND_PVC].add(f"{name}-gcs-pvc")
        changed = False
        for kind, keep in desired.items():
            for item in self.store.list(kind, namespace, label_selector={C.LABEL_CLUSTER: name}):
                if not self._owned_by_cluster(item, cluster):
                    # e.g. RayService's per-cluster Serve Service carries the
                    # cluster label but is owned by the RayService; do not
                    # prune another controller's child.
                    continue
                item_name = item["metadata"]["name"]
                if item_name in keep:
                    continue
                if kind == C.KIND_PVC:
                    # Only prune the operator-managed GCS PVC; never touch a
                    # user PVC that happens to carry the cluster label.
                    if item_name != f"{name}-gcs-pvc":
                        continue
                    if (item.get("metadata", {}).get("annotations") or {}).get("minikuberay.io/retain") == "true":
                        continue
                try:
                    self.store.delete(kind, item_name, namespace, force=True)
                    changed = True
                except Exception:
                    pass
        return changed

    # ---------------------------------------------------------------------- pods
    @staticmethod
    def _owned_by_cluster(item: dict, cluster: dict) -> bool:
        cluster_uid = (cluster.get("metadata") or {}).get("uid")
        return any(
            ref.get("uid") == cluster_uid
            for ref in (item.get("metadata") or {}).get("ownerReferences") or []
        )

    def _all_pods(self, cluster: dict) -> list[dict]:
        return [
            pod
            for pod in self.store.list(C.KIND_POD, obj_namespace(cluster), label_selector={C.LABEL_CLUSTER: obj_name(cluster)})
            if not deletion_timestamp(pod)
        ]

    def _delete_all_pods(self, cluster: dict) -> None:
        namespace = obj_namespace(cluster)
        for pod in self._all_pods(cluster):
            try:
                self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                self.runtime.unregister_pod(namespace, obj_name(cluster), pod["metadata"]["name"])
            except Exception:
                pass

    def _head_pods(self, cluster: dict) -> list[dict]:
        return [pod for pod in self._all_pods(cluster) if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_HEAD]

    def _worker_pods(self, cluster: dict, group_name: str | None = None) -> list[dict]:
        pods = [pod for pod in self._all_pods(cluster) if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
        if group_name is not None:
            pods = [pod for pod in pods if (pod["metadata"].get("labels") or {}).get(C.LABEL_GROUP) == group_name]
        return pods

    def _ensure_pods(self, cluster: dict, spec_hash: str) -> bool:
        changed = False
        namespace = obj_namespace(cluster)
        spec = cluster.get("spec") or {}
        scheduler_name = scheduler_name_for(self.config.batch_scheduler)
        workers_to_clear: set[str] = set()
        failure_message = ""

        head_pods = self._head_pods(cluster)
        if len(head_pods) > 1:
            for pod in head_pods[1:]:
                self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                self.runtime.unregister_pod(namespace, obj_name(cluster), pod["metadata"]["name"])
                changed = True
            head_pods = head_pods[:1]
        if not head_pods:
            annotations = (cluster.get("metadata") or {}).get("annotations") or {}
            skip_restart = (
                str(annotations.get(C.ANNOTATION_DISABLE_HEAD_RESTART, "")).lower() == "true"
                and is_condition_true(cluster.get("status") or {}, C.COND_PROVISIONED)
            )
            if not skip_restart:
                try:
                    pod = build_head_pod(cluster, scheduler_name=scheduler_name)
                    pod["metadata"]["labels"]["ray.io/spec-hash"] = spec_hash
                    self.store.create(C.KIND_POD, pod, namespace)
                    self.recorder.normal(cluster, "CreatedHeadPod", f"Created head Pod {pod['metadata']['name']}")
                    changed = True
                except Exception as exc:
                    self.recorder.warning(cluster, "FailedToCreateHeadPod", str(exc))
                    self._record_replica_failure(namespace, obj_name(cluster), str(exc))
                    return changed
        else:
            head = head_pods[0]
            annotations = (cluster.get("metadata") or {}).get("annotations") or {}
            skip_restart = str(annotations.get(C.ANNOTATION_DISABLE_HEAD_RESTART, "")).lower() == "true"
            if pod_phase(head) in ("Failed", "Succeeded") and not skip_restart:
                self.store.delete(C.KIND_POD, head["metadata"]["name"], namespace, force=True)
                self.runtime.unregister_pod(namespace, obj_name(cluster), head["metadata"]["name"])
                changed = True

        for group in spec.get("workerGroupSpecs") or []:
            group_name = group.get("groupName", "worker")
            pods = self._worker_pods(cluster, group_name)
            if int(group.get("numOfHosts", 1) or 1) > 1:
                group_changed, cleared, group_failure = self._ensure_multi_host_pods(
                    cluster, group, pods, spec_hash, scheduler_name
                )
                changed |= group_changed
                workers_to_clear.update(cleared)
                if group_failure:
                    failure_message = group_failure
                continue

            # Remove failed pods; a replacement is created in the same pass.
            for pod in list(pods):
                if pod_phase(pod) in ("Failed", "Succeeded"):
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                    self.runtime.unregister_pod(namespace, obj_name(cluster), pod["metadata"]["name"])
                    pods.remove(pod)
                    changed = True

            to_delete = list((group.get("scaleStrategy") or {}).get("workersToDelete") or [])
            if to_delete:
                removed = set()
                for pod_name in to_delete:
                    try:
                        target = self.store.get(C.KIND_POD, pod_name, namespace)
                    except Exception:
                        removed.add(pod_name)
                        continue
                    target_labels = target.get("metadata", {}).get("labels") or {}
                    if (
                        target_labels.get(C.LABEL_CLUSTER) != obj_name(cluster)
                        or target_labels.get(C.LABEL_GROUP) != group_name
                        or target_labels.get(C.LABEL_NODE_TYPE) != C.NODE_TYPE_WORKER
                    ):
                        # Never let a stale/forged scaleStrategy delete an
                        # unrelated Pod in the namespace.
                        removed.add(pod_name)
                        continue
                    try:
                        self.store.delete(C.KIND_POD, pod_name, namespace, force=True)
                        self.runtime.unregister_pod(namespace, obj_name(cluster), pod_name)
                        removed.add(pod_name)
                        changed = True
                    except Exception:
                        removed.add(pod_name)
                pods = [pod for pod in pods if pod["metadata"]["name"] not in removed]
                workers_to_clear.update(removed)

            desired = group_desired_pods(group)
            diff = desired - len(pods)
            if diff > 0:
                used = set()
                for pod in pods:
                    labels = pod["metadata"].get("labels") or {}
                    try:
                        used.add(int(labels.get(C.LABEL_WORKER_REPLICA_INDEX, -1)))
                    except ValueError:
                        pass
                next_index = 0
                for _ in range(diff):
                    while next_index in used:
                        next_index += 1
                    used.add(next_index)
                    try:
                        pod = build_worker_pod(cluster, group, next_index, scheduler_name=scheduler_name)
                        pod["metadata"]["labels"]["ray.io/spec-hash"] = spec_hash
                        self.store.create(C.KIND_POD, pod, namespace)
                        self.recorder.normal(cluster, "CreatedWorkerPod", f"Created worker Pod {pod['metadata']['name']}")
                        changed = True
                    except Exception as exc:
                        failure_message = str(exc)
                        self.recorder.warning(cluster, "FailedToCreateWorkerPod", str(exc))
                        break
            elif diff < 0:
                autoscaling = bool(spec.get("enableInTreeAutoscaling"))
                if autoscaling and not self.config.enable_random_pod_delete:
                    # The autoscaler is the only entity that chooses victims.
                    continue
                # Keep the lowest replica indices (matching the multi-host
                # path) and delete the highest ones.
                desired_count = max(0, len(pods) + diff)
                for pod in pods[desired_count:]:
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                    self.runtime.unregister_pod(namespace, obj_name(cluster), pod["metadata"]["name"])
                    self.recorder.normal(cluster, "DeletedWorkerPod", f"Deleted surplus worker Pod {pod['metadata']['name']}")
                    changed = True

        # A worker group removed from the spec must not leave its Pods behind.
        # Labels (not Pod names) are the source of truth for group membership.
        known_groups = {
            group.get("groupName") or f"worker-group-{index}"
            for index, group in enumerate(spec.get("workerGroupSpecs") or [])
        }
        for pod in self._worker_pods(cluster):
            group_name = (pod["metadata"].get("labels") or {}).get(C.LABEL_GROUP)
            if group_name and group_name not in known_groups:
                try:
                    self.store.delete(C.KIND_POD, pod["metadata"]["name"], namespace, force=True)
                    self.runtime.unregister_pod(namespace, obj_name(cluster), pod["metadata"]["name"])
                    self.recorder.normal(
                        cluster,
                        "DeletedWorkerPod",
                        f"Deleted worker Pod {pod['metadata']['name']} from removed group {group_name}",
                    )
                    changed = True
                except Exception:
                    pass

        if workers_to_clear:
            try:
                current = self.store.get(C.KIND_RAYCLUSTER, obj_name(cluster), namespace)
                for group in (current.get("spec") or {}).get("workerGroupSpecs") or []:
                    scale = group.setdefault("scaleStrategy", {})
                    scale["workersToDelete"] = [name for name in scale.get("workersToDelete") or [] if name not in workers_to_clear]
                self.store.update(C.KIND_RAYCLUSTER, current)
                changed = True
            except Exception:
                pass
        self._record_replica_failure(namespace, obj_name(cluster), failure_message)
        return changed

    def _record_replica_failure(self, namespace: str, name: str, message: str) -> None:
        if not hasattr(self, "_replica_failures"):
            self._replica_failures: dict[tuple[str, str], str] = {}
        self._replica_failures[(namespace, name)] = message

    def _ensure_multi_host_pods(
        self,
        cluster: dict,
        group: dict,
        pods: list[dict],
        spec_hash: str,
        scheduler_name: str | None = None,
    ) -> tuple[bool, set[str], str]:
        """Reconcile a worker group whose replicas span several Pods.

        KubeRay groups Pods by ``ray.io/worker-group-replica-name``.  A replica is
        an all-or-nothing unit: if one host is missing, failed, or surplus, the
        whole replica is replaced.  Scaling moves whole replicas, never single
        hosts.  This mirrors ``reconcileMultiHostWorkerGroup`` in the real
        controller (simplified to deterministic replica names).
        """
        namespace = obj_namespace(cluster)
        cluster_name = obj_name(cluster)
        group_name = group.get("groupName", "worker")
        hosts = max(1, int(group.get("numOfHosts", 1) or 1))
        desired = group_replicas(group)
        changed = False
        failure = ""
        cleared: set[str] = set()

        replicas: dict[str, list[dict]] = {}
        for pod in pods:
            labels = pod["metadata"].get("labels") or {}
            replica_name = labels.get(C.LABEL_WORKER_REPLICA_NAME) or (
                f"{cluster_name}-{group_name}-{labels.get(C.LABEL_WORKER_REPLICA_INDEX, '0')}"
            )
            replicas.setdefault(replica_name, []).append(pod)

        def replica_index(_name: str, replica_pods: list[dict]) -> int:
            labels = replica_pods[0]["metadata"].get("labels") or {}
            try:
                return int(labels.get(C.LABEL_WORKER_REPLICA_INDEX, -1))
            except (TypeError, ValueError):
                return -1

        def delete_replica(_name: str, replica_pods: list[dict]) -> None:
            nonlocal changed
            for pod in replica_pods:
                pod_name = pod["metadata"]["name"]
                try:
                    self.store.delete(C.KIND_POD, pod_name, namespace, force=True)
                    self.runtime.unregister_pod(namespace, cluster_name, pod_name)
                    changed = True
                except Exception:
                    pass

        # The autoscaler names individual Pods.  In a multi-host group, deleting
        # any host means the replica can no longer form a complete slice, so the
        # entire replica is the victim.
        to_delete = set((group.get("scaleStrategy") or {}).get("workersToDelete") or [])
        if to_delete:
            for replica_name, replica_pods in list(replicas.items()):
                if to_delete & {pod["metadata"]["name"] for pod in replica_pods}:
                    delete_replica(replica_name, replica_pods)
                    del replicas[replica_name]
            # Clear names that no longer refer to a live Pod as well, otherwise
            # the autoscaler's stale list would be rewritten forever.
            cleared.update(to_delete)

        # Unhealthy or incomplete replicas are removed as a unit.
        for replica_name, replica_pods in list(replicas.items()):
            unhealthy = any(pod_phase(pod) in ("Failed", "Succeeded") for pod in replica_pods)
            if unhealthy or len(replica_pods) != hosts:
                delete_replica(replica_name, replica_pods)
                del replicas[replica_name]

        # Scale down whole replicas, highest index first.
        ordered = sorted(replicas.items(), key=lambda item: replica_index(item[0], item[1]))
        for replica_name, replica_pods in ordered[desired:]:
            delete_replica(replica_name, replica_pods)
            del replicas[replica_name]

        # Fill missing replica indices; each new replica is created host by host
        # with the same replica-name label.
        present = {replica_index(replica_name, replica_pods) for replica_name, replica_pods in replicas.items()}
        for index in range(desired):
            if index in present:
                continue
            replica_name = f"{cluster_name}-{group_name}-{index}"
            for host_index in range(hosts):
                try:
                    pod = build_worker_pod(
                        cluster,
                        group,
                        index,
                        host_index,
                        replica_name=replica_name,
                        scheduler_name=scheduler_name,
                    )
                    pod["metadata"]["labels"]["ray.io/spec-hash"] = spec_hash
                    self.store.create(C.KIND_POD, pod, namespace)
                    self.recorder.normal(cluster, "CreatedWorkerPod", f"Created worker Pod {pod['metadata']['name']}")
                    changed = True
                except Exception as exc:
                    failure = str(exc)
                    self.recorder.warning(cluster, "FailedToCreateWorkerPod", str(exc))
                    self._record_replica_failure(namespace, cluster_name, str(exc))
                    break
        return changed, cleared, failure

    # ----------------------------------------------------------------- autoscale
    def _recover_failed_resizes(self, cluster: dict) -> bool:
        """Roll back any Pod whose kubelet marked the resize as failed.

        This deliberately does not depend on the IPPR annotation, autoscaling
        being enabled, or the RayCluster still validating: a half-applied
        resize is a data-plane inconsistency that must be repaired first.
        """
        changed = False
        for pod in self._worker_pods(cluster):
            if status_of(pod).get("state") != "failed":
                continue
            updated, rolled_back = rollback(pod, clock=self.clock)
            if not rolled_back:
                continue
            self.store.update(C.KIND_POD, updated)
            self.recorder.warning(
                cluster,
                "AutoscalerResizeRolledBack",
                f"{pod['metadata']['name']}: restored previous resources after "
                f"{status_of(pod).get('message', 'resize failure')}",
            )
            self.metrics.autoscaler_action(
                "resize-rollback", obj_namespace(cluster), obj_name(cluster), status_of(pod).get("group", "")
            )
            changed = True
        return changed

    def _autoscale(self, cluster: dict) -> bool:
        spec = cluster.get("spec") or {}
        changed = self._recover_failed_resizes(cluster)
        if not spec.get("enableInTreeAutoscaling") or spec.get("suspend"):
            return changed
        policy = AutoscalerPolicy(self.runtime)
        decisions = policy.decide(cluster, pods=self._worker_pods(cluster))
        ippr_configs, _ = parse_ippr(cluster)
        cluster_changed = False
        for decision in decisions:
            group = next((item for item in spec.get("workerGroupSpecs") or [] if item.get("groupName") == decision.group), None)
            if not group:
                continue
            if decision.action == "scale-up" and decision.desired != group_replicas(group):
                group["replicas"] = decision.desired
                changed = True
                cluster_changed = True
                self.recorder.normal(cluster, "AutoscalerScaleUp", f"{decision.group}: {decision.current} -> {decision.desired} ({decision.reason})")
            elif decision.action == "scale-down" and (
                decision.desired != group_replicas(group) or decision.workers_to_delete
            ):
                group["replicas"] = decision.desired
                group.setdefault("scaleStrategy", {})["workersToDelete"] = decision.workers_to_delete or []
                changed = True
                cluster_changed = True
                self.recorder.normal(cluster, "AutoscalerScaleDown", f"{decision.group}: {decision.current} -> {decision.desired} ({decision.reason})")
            elif decision.action == "resize-up" and decision.pod_name:
                config = ippr_configs.get(decision.group)
                if config is None:
                    continue
                try:
                    pod = self.store.get(C.KIND_POD, decision.pod_name, obj_namespace(cluster))
                except Exception:
                    continue
                updated = apply_resize(
                    pod,
                    config,
                    decision.target_cpu or 0,
                    decision.target_memory or 0,
                    clock=self.clock,
                )
                self.store.update(C.KIND_POD, updated)
                changed = True
                self.recorder.normal(
                    cluster,
                    "AutoscalerResizeUp",
                    f"{decision.pod_name}: cpu {decision.current_cpu}m -> {decision.target_cpu}m "
                    f"({decision.reason})",
                )
            if decision.action != "none":
                self.metrics.autoscaler_action(decision.action, obj_namespace(cluster), obj_name(cluster), decision.group)
        # A Pod resize already wrote the Pod; writing the RayCluster on top of
        # it would bump resourceVersion for no reason and hide convergence
        # bugs.  Only the replica/scaleStrategy edits need a CR update.
        if cluster_changed:
            merged = deep_copy(cluster)
            merged["spec"] = spec
            merged["status"] = self.store.get(C.KIND_RAYCLUSTER, obj_name(cluster), obj_namespace(cluster)).get("status", {})
            self.store.update(C.KIND_RAYCLUSTER, merged)
        return changed

    # -------------------------------------------------------------------- status
    def _stored_hash(self, cluster: dict) -> str | None:
        # Persist the applied hash on the CR so deleting the head Pod does not
        # lose the upgrade baseline and strand old worker Pods.
        annotations = (cluster.get("metadata") or {}).get("annotations") or {}
        if annotations.get(C.ANNOTATION_SPEC_HASH):
            return annotations[C.ANNOTATION_SPEC_HASH]
        for pod in self._all_pods(cluster):
            value = (pod["metadata"].get("labels") or {}).get("ray.io/spec-hash")
            if value:
                return value
        return None

    def _remember_hash(self, cluster: dict, spec_hash: str) -> bool:
        annotations = (cluster.get("metadata") or {}).get("annotations") or {}
        if annotations.get(C.ANNOTATION_SPEC_HASH) == spec_hash:
            return False
        try:
            current = self.store.get(C.KIND_RAYCLUSTER, obj_name(cluster), obj_namespace(cluster))
        except Exception:
            return False
        current.setdefault("metadata", {}).setdefault("annotations", {})[C.ANNOTATION_SPEC_HASH] = spec_hash
        self.store.update(C.KIND_RAYCLUSTER, current)
        return True

    @staticmethod
    def _spec_hash(cluster: dict) -> str:
        payload = deep_copy(cluster.get("spec") or {})
        for group in payload.get("workerGroupSpecs") or []:
            # Replica bounds are autoscaler-owned; changing them must not
            # trigger a Recreate rollout (KubeRay excludes the same fields).
            group.pop("replicas", None)
            group.pop("minReplicas", None)
            group.pop("maxReplicas", None)
            group.get("scaleStrategy", {}).pop("workersToDelete", None)
        return stable_hash(payload, 16)

    def _desired_resources(self, cluster: dict) -> dict[str, float]:
        spec = cluster.get("spec") or {}
        head = spec.get("headGroupSpec") or {}
        head_containers = (head.get("template") or {}).get("spec", {}).get("containers") or []
        totals = {"cpu": 0.0, "memory": 0.0, "nvidia.com/gpu": 0.0, "google.com/tpu": 0.0}
        for container in head_containers:
            requests = container.get("resources", {}).get("requests", {})
            for resource in totals:
                totals[resource] += resource_quantity(requests, resource)
        for group in spec.get("workerGroupSpecs") or []:
            capacity = worker_group_capacity(group)
            for resource in totals:
                totals[resource] += capacity.get(resource, 0.0) * group_desired_pods(group)
        return totals

    def _write_status(self, cluster: dict) -> bool:
        namespace = obj_namespace(cluster)
        name = obj_name(cluster)
        try:
            current = self.store.get(C.KIND_RAYCLUSTER, name, namespace)
        except Exception:
            return False
        status = deep_copy(current.get("status") or {})

        pods = self._all_pods(cluster)
        head_pods = [pod for pod in pods if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_HEAD]
        worker_pods = [pod for pod in pods if (pod["metadata"].get("labels") or {}).get(C.LABEL_NODE_TYPE) == C.NODE_TYPE_WORKER]
        desired = desired_worker_replicas(cluster)
        ready_workers = sum(1 for pod in worker_pods if pod_is_ready(pod))
        available_workers = sum(1 for pod in worker_pods if pod_phase(pod) == "Running")
        head_ready = any(pod_is_ready(pod) for pod in head_pods)
        all_ready = head_ready and ready_workers >= desired
        suspended = bool((cluster.get("spec") or {}).get("suspend"))
        status["state"] = "suspended" if suspended else ("ready" if all_ready else "")
        if not suspended:
            for condition in status.get("conditions") or []:
                if condition.get("type") in (C.COND_SUSPENDING, C.COND_SUSPENDED):
                    condition["status"] = "False"
                    condition["reason"] = "RayClusterResumed"
        status["desiredWorkerReplicas"] = desired
        status["minWorkerReplicas"] = min_worker_replicas(cluster.get("spec") or {})
        status["maxWorkerReplicas"] = max_worker_replicas(cluster.get("spec") or {})
        status["readyWorkerReplicas"] = ready_workers
        status["availableWorkerReplicas"] = available_workers
        resources = self._desired_resources(cluster)
        status["desiredCPU"] = format_cpu(int(round(resources["cpu"])))
        status["desiredMemory"] = format_memory(int(round(resources["memory"])))
        status["desiredGPU"] = status_quantity(resources["nvidia.com/gpu"], "gpu")
        status["desiredTPU"] = status_quantity(resources.get("google.com/tpu", 0.0), "tpu")

        try:
            service = self.store.get(C.KIND_SERVICE, head_service_name(cluster), namespace)
            raw_ip = service.get("spec", {}).get("clusterIP")
            service_ip = raw_ip if raw_ip and raw_ip != "None" else f"{head_service_name(cluster)}.{namespace}.svc.{self.config.cluster_domain}"
            status["endpoints"] = {
                "client": f"{service_ip}:{C.PORT_CLIENT}",
                "dashboard": f"{service_ip}:{C.PORT_DASHBOARD}",
                "metrics": f"{service_ip}:{C.PORT_METRICS}",
                "serve": f"{service_ip}:{C.PORT_SERVE}",
                "gcs-server": f"{service_ip}:{C.PORT_GCS}",
            }
            status["head"] = {
                "podIP": head_pods[0].get("status", {}).get("podIP", "") if head_pods else "",
                "serviceIP": service_ip,
                "podName": head_pods[0]["metadata"]["name"] if head_pods else "",
                "serviceName": head_service_name(cluster),
            }
        except Exception:
            status.setdefault("endpoints", {})

        proxy = {"metadata": current["metadata"], "status": status}
        set_condition(
            proxy,
            C.COND_HEAD_READY,
            head_ready,
            "HeadPodRunningAndReady" if head_ready else "HeadPodNotFound",
            "" if head_ready else "head Pod is not ready",
            clock=self.clock,
        )
        if all_ready:
            set_condition(proxy, C.COND_PROVISIONED, True, "AllPodRunningAndReadyFirstTime", "all Ray Pods are ready", clock=self.clock)
        elif not is_condition_true(status, C.COND_PROVISIONED):
            set_condition(proxy, C.COND_PROVISIONED, False, "RayClusterPodsProvisioning", "waiting for all Ray Pods", clock=self.clock)
        failure = getattr(self, "_replica_failures", {}).get((namespace, name), "")
        if failure:
            set_condition(proxy, C.COND_REPLICA_FAILURE, True, "FailedToCreatePod", failure, clock=self.clock)
        elif is_condition_true(proxy, C.COND_REPLICA_FAILURE):
            set_condition(proxy, C.COND_REPLICA_FAILURE, False, "ReplicaFailureResolved", "", clock=self.clock)
        status = proxy["status"]
        # A previously invalid spec may have written status.reason/message.
        # Once validation passes again, keeping them would make a healthy
        # cluster look permanently broken to operators and GitOps tooling.
        status.pop("reason", None)
        status.pop("message", None)
        status["observedGeneration"] = cluster["metadata"].get("generation", 1)
        status["readyWorkerReplicas"] = ready_workers
        changed = self._commit_status(current, status)
        if changed:
            self.metrics.cluster_ready(namespace, name, all_ready)
        return changed

    def _commit_status(self, current: dict, status: dict) -> bool:
        old = deep_copy(current.get("status") or {})
        old_cmp = {key: value for key, value in old.items() if key != "lastUpdateTime"}
        new_cmp = {key: value for key, value in status.items() if key != "lastUpdateTime"}
        if canonical_json(old_cmp) == canonical_json(new_cmp):
            return False
        status = deep_copy(status)
        status["lastUpdateTime"] = self._now()
        updated = deep_copy(current)
        updated["status"] = status
        self.store.update_status(updated)
        return True

    def _now(self) -> str:
        from ..clock import iso

        return iso(self.clock.now())
