"""Controller runtime primitives: rate-limited queue, controllers, operator."""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .. import constants as C
from ..clock import Clock, RealClock
from ..errors import ApiError, Conflict, NotFound
from ..events import EventRecorder
from ..history import HistoryServer, HistoryStore
from ..kube.client import LocalClient
from ..metrics import OperatorMetrics
from ..runtime.cluster_runtime import RayRuntime
from ..store.store import ApiStore
from ..tracing import Tracer

DEFAULT_FEATURE_GATES = {
    "RayClusterStatusConditions": True,
    "RayJobDeletionPolicy": True,
    "RayMultiHostIndexing": True,
    "RayServiceIncrementalUpgrade": True,
    "RayCronJob": False,
    "SidecarSubmitterRestart": False,
    "RayClusterNetworkPolicy": False,
    "GCSFaultToleranceEmbeddedStorage": False,
    "RayClusterMTLS": False,
    "RayClusterHistoryServer": False,
    "KubernetesWAS": False,
    "GCSFaultToleranceActivePassiveHead": False,
    "RayNodeEventForwarder": False,
    "NodeLabelDelivery": False,
}


@dataclass
class OperatorConfig:
    namespace: str | None = None
    cluster_domain: str = C.DEFAULT_CLUSTER_DOMAIN
    default_image: str = C.DEFAULT_IMAGE
    autoscaler_enabled: bool = True
    batch_scheduler: str = ""  # "", volcano, yunikorn, scheduler-plugins, was (teaching alias)
    feature_gates: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_FEATURE_GATES))
    resync_seconds: float = C.DEFAULT_RESYNC_SECONDS
    reconcile_interval: float = 0.05
    max_rounds: int = 200
    delete_rayjob_cr_after_finishes: bool = False
    enable_random_pod_delete: bool = False
    deterministic_names: bool = True
    execute_jobs: bool = False
    log_level: str = "INFO"
    history_enabled: bool = True
    history_path: str | None = None
    history_max_events_per_session: int = 2000
    trace_max_spans: int = 5000

    def enabled(self, gate: str) -> bool:
        return bool(self.feature_gates.get(gate, False))


class RateLimiter:
    def __init__(self, base: float = 0.005, max_delay: float = 1000.0):
        self.base = base
        self.max_delay = max_delay
        self._failures: dict[Any, int] = {}
        self._lock = threading.Lock()

    def when(self, key: Any) -> float:
        with self._lock:
            failures = self._failures.get(key, 0)
            return 0.0 if failures == 0 else min(self.max_delay, self.base * (2 ** (failures - 1)))

    def forget(self, key: Any) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def failure(self, key: Any) -> None:
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1

    def num_failures(self, key: Any) -> int:
        return self._failures.get(key, 0)


class WorkQueue:
    """Deduplicating, delayed work queue in the style of client-go."""

    def __init__(self, clock: Clock | None = None, rate_limiter: RateLimiter | None = None):
        self.clock = clock or RealClock()
        self.rate_limiter = rate_limiter or RateLimiter()
        self._heap: list[tuple[float, int, Any]] = []
        self._queued: set[Any] = set()
        self._processing: set[Any] = set()
        self._sequence = itertools.count()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._shutdown = False

    def add(self, key: Any, delay: float = 0.0) -> None:
        with self._lock:
            if key in self._queued or key in self._processing:
                return
            ready_at = self.clock.monotonic() + max(0.0, delay)
            heapq.heappush(self._heap, (ready_at, next(self._sequence), key))
            self._queued.add(key)
            self._changed.notify_all()

    def add_rate_limited(self, key: Any) -> None:
        self.add(key, self.rate_limiter.when(key))

    def get(self, timeout: float | None = None) -> Any | None:
        deadline = None if timeout is None else self.clock.monotonic() + timeout
        with self._lock:
            while not self._shutdown:
                now = self.clock.monotonic()
                if self._heap and self._heap[0][0] <= now:
                    _, _, key = heapq.heappop(self._heap)
                    self._queued.discard(key)
                    self._processing.add(key)
                    return key
                wait = None if deadline is None else max(0.0, deadline - now)
                if deadline is not None and wait == 0:
                    return None
                if self._heap:
                    wait = min(wait, max(0.0, self._heap[0][0] - now)) if wait is not None else max(0.0, self._heap[0][0] - now)
                self._changed.wait(timeout=wait)
            return None

    def done(self, key: Any, *, success: bool = True) -> None:
        with self._lock:
            self._processing.discard(key)
            if success:
                self.rate_limiter.forget(key)
            else:
                self.rate_limiter.failure(key)
            self._changed.notify_all()

    def empty(self) -> bool:
        with self._lock:
            return not self._heap and not self._processing

    def pending(self) -> int:
        with self._lock:
            return len(self._heap) + len(self._processing)

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._changed.notify_all()


class Controller:
    """Base class.  A controller reconciles one object at a time, idempotently."""

    name = "controller"
    kind = ""

    def __init__(
        self,
        *,
        store: ApiStore,
        client: LocalClient,
        clock: Clock,
        runtime: RayRuntime,
        recorder: EventRecorder,
        metrics: OperatorMetrics,
        config: OperatorConfig,
        tracer: Tracer | None = None,
    ):
        self.store = store
        self.client = client
        self.clock = clock
        self.runtime = runtime
        self.recorder = recorder
        self.metrics = metrics
        self.config = config
        self.tracer = tracer or Tracer(clock)
        self.queue = WorkQueue(clock)

    def reconcile(self, obj: dict) -> bool:
        raise NotImplementedError

    def enqueue(self, obj: dict) -> None:
        self.queue.add((obj["kind"], obj["metadata"].get("namespace", "default"), obj["metadata"]["name"]))

    def reconcile_all(self) -> bool:
        changed = False
        objects = self.store.list(self.kind, self.config.namespace)
        for obj in objects:
            started = time.perf_counter()
            result = "success"
            metadata = obj.get("metadata", {})
            with self.tracer.span(
                "reconcile",
                controller=self.name,
                kind=self.kind,
                namespace=metadata.get("namespace", "default"),
                name=metadata.get("name", ""),
                generation=metadata.get("generation", 1),
            ) as span:
                try:
                    object_changed = self.reconcile(obj)
                    changed = object_changed or changed
                    span.set_attribute("changed", object_changed)
                except NotFound:
                    result = "not_found"
                except Conflict:
                    result = "conflict"
                    self.queue.add_rate_limited((obj["kind"], metadata.get("namespace"), metadata.get("name")))
                except (ApiError, ValueError, KeyError) as exc:
                    result = "error"
                    self.recorder.warning(obj, "ReconcileError", str(exc))
                except Exception as exc:  # pragma: no cover - defensive
                    result = "error"
                    self.recorder.warning(obj, "ReconcileError", f"{type(exc).__name__}: {exc}")
                finally:
                    duration = time.perf_counter() - started
                    span.set_attribute("result", result)
                    span.set_attribute("duration_perf_ms", round(duration * 1000.0, 3))
                    if result in ("error", "conflict"):
                        span.set_status("ERROR", result)
                        span.add_event("reconcile." + result)
                    self.metrics.reconcile(self.name, result, duration)
        return changed

    # ----------------------------------------------------------------- helpers
    def ensure(self, kind: str, obj: dict) -> tuple[dict, bool]:
        """Create or update a child resource, returning (object, changed)."""
        namespace = obj["metadata"].get("namespace", "default")
        name = obj["metadata"]["name"]
        try:
            current = self.store.get(kind, name, namespace)
        except NotFound:
            return self.store.create(kind, obj, namespace), True
        # The API server (or our EndpointController) assigns clusterIPs.  The
        # controller must not erase them on the next ensure pass.
        if kind == C.KIND_SERVICE:
            current_spec = current.get("spec") or {}
            desired_spec = obj.setdefault("spec", {})
            for key in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy", "healthCheckNodePort"):
                if current_spec.get(key) and not desired_spec.get(key):
                    desired_spec[key] = current_spec[key]
        # Controller-owned fields win; compare without resourceVersion.
        desired = _strip_volatile(obj)
        actual = _strip_volatile(current)
        if desired != actual:
            merged = dict(current)
            merged.update({key: value for key, value in obj.items() if key != "status"})
            # Preserve status managed by another controller.
            merged["status"] = current.get("status", {})
            return self.store.update(kind, merged), True
        return current, False

    def delete_child(self, kind: str, name: str, namespace: str) -> bool:
        try:
            self.store.delete(kind, name, namespace)
            return True
        except NotFound:
            return False


def _strip_volatile(obj: dict) -> dict:
    from ..util import deep_copy

    copy = deep_copy(obj)
    metadata = copy.get("metadata", {})
    for key in ("resourceVersion", "uid", "creationTimestamp", "deletionTimestamp", "generation", "managedFields"):
        metadata.pop(key, None)
    copy.pop("status", None)
    return copy


class Operator:
    """Owns all controllers and the simulated kubelet; one process, one loop."""

    def __init__(self, *, store: ApiStore | None = None, clock: Clock | None = None, config: OperatorConfig | None = None, runtime: RayRuntime | None = None):
        self.clock = clock or RealClock()
        self.config = config or OperatorConfig()
        self.store = store or ApiStore(self.clock)
        self.client = LocalClient(self.store)
        self.runtime = runtime or RayRuntime(self.clock)
        self.history_store = HistoryStore(
            self.clock,
            max_events_per_session=self.config.history_max_events_per_session,
            path=self.config.history_path if self.config.history_enabled else None,
        ) if self.config.history_enabled else None
        self.history = HistoryServer(self.history_store) if self.history_store else None
        self.recorder = EventRecorder(self.store, self.clock, history=self.history_store)
        self.metrics = OperatorMetrics()
        self.tracer = Tracer(self.clock, max_spans=self.config.trace_max_spans)
        self.controllers: list[Controller] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._build_controllers()

    def _build_controllers(self) -> None:
        from .batch import BatchJobController, EndpointController, GarbageCollector, Kubelet
        from .raycluster import RayClusterController
        from .raycronjob import RayCronJobController
        from .rayjob import RayJobController
        from .rayservice import RayServiceController

        kwargs = dict(
            store=self.store,
            client=self.client,
            clock=self.clock,
            runtime=self.runtime,
            recorder=self.recorder,
            metrics=self.metrics,
            config=self.config,
            tracer=self.tracer,
        )
        self.kubelet = Kubelet(**kwargs)
        self.endpoints = EndpointController(**kwargs)
        self.batch = BatchJobController(**kwargs)
        self.raycluster = RayClusterController(**kwargs)
        self.rayjob = RayJobController(**kwargs)
        self.rayservice = RayServiceController(**kwargs)
        self.raycronjob = RayCronJobController(**kwargs)
        self.gc = GarbageCollector(**kwargs)
        self.controllers = [
            self.raycluster,
            self.rayjob,
            self.rayservice,
            self.raycronjob,
            self.batch,
        ]

    def register(self, controller: Controller) -> None:
        self.controllers.append(controller)

    def run_once(self) -> bool:
        with self.tracer.span("operator.round") as span:
            before = self.store.resource_version
            changed = self._run_once()
            span.set_attribute("changed", changed)
            span.set_attribute("resourceVersion.before", before)
            span.set_attribute("resourceVersion.after", self.store.resource_version)
            span.set_attribute("tracer.enabled", True)
            return changed

    def _run_once(self) -> bool:
        before = self.store.resource_version
        clusters_before = self._cluster_map()
        self._snapshot_clusters(clusters_before)
        self.kubelet.step()
        # Services/EndpointSlices must converge before status is calculated.
        self.endpoints.reconcile_all()
        for controller in self.controllers:
            if controller is self.raycronjob and not self.config.enabled("RayCronJob"):
                continue
            controller.reconcile_all()
        self.gc.run()
        clusters_after = self._cluster_map()
        self._snapshot_clusters(clusters_after)
        if self.history_store is not None:
            # Sessions outlive the RayCluster: the History Server is most useful
            # precisely when the CR and Pods are gone.
            for key, cluster in clusters_before.items():
                if key not in clusters_after:
                    self.history_store.mark_deleted(*key)
        return self.store.resource_version != before

    def _cluster_map(self) -> dict[tuple[str, str], dict]:
        return {
            (cluster["metadata"].get("namespace", "default"), cluster["metadata"]["name"]): cluster
            for cluster in self.store.list(C.KIND_RAYCLUSTER, self.config.namespace)
        }

    def _snapshot_clusters(self, clusters: dict[tuple[str, str], dict]) -> None:
        if self.history_store is None:
            return
        runtime_snapshot = self.runtime.snapshot().get("clusters", {})
        for (namespace, name), cluster in clusters.items():
            self.history_store.snapshot(cluster, runtime=runtime_snapshot.get(f"{namespace}/{name}"))

    def settle(self, *, max_rounds: int | None = None, advance: float = 0.0) -> int:
        """Run until no object changes; returns rounds used.

        ``advance`` optionally moves a ManualClock by that many seconds per
        round, which is how tests exercise timers, TTLs and idle scale-down.
        """
        rounds = 0
        limit = max_rounds or self.config.max_rounds
        for _ in range(limit):
            rounds += 1
            changed = self.run_once()
            if advance:
                try:
                    self.clock.advance(advance)  # type: ignore[attr-defined]
                except AttributeError:
                    time.sleep(advance)
            if not changed:
                return rounds
        return rounds

    def start(self, *, interval: float | None = None) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        interval = interval or self.config.reconcile_interval

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception as exc:  # pragma: no cover - process safety
                    print(f"operator loop error: {exc}")
                self._stop_event.wait(interval)

        self._thread = threading.Thread(target=loop, name="minikuberay-operator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.history_store is not None:
            self.history_store.save()
        self.store.close()

    def snapshot(self) -> dict:
        return {
            "resourceVersion": self.store.resource_version,
            "runtime": self.runtime.snapshot(),
            "objects": {
                kind: self.store.list(kind)
                for kind in (C.KIND_RAYCLUSTER, C.KIND_RAYJOB, C.KIND_RAYSERVICE, C.KIND_RAYCRONJOB, C.KIND_POD, C.KIND_SERVICE)
            },
        }
