"""A deterministic simulation of the parts of Ray the operator talks to.

The real KubeRay operator does not execute user code.  It creates Pods and then
talks to the Ray dashboard / GCS / autoscaler.  MiniKubeRay models that boundary
as ``RayRuntime``:

* Pods become Ray nodes when the simulated kubelet reports them Running.
* Ray jobs have the same five observable states as Ray Job Submission.
* Serve applications move DEPLOYING -> RUNNING after a configurable delay.
* The autoscaler reads a demand queue and per-node idle timestamps.

No user entrypoint is executed unless ``ProcessExecutor`` is explicitly used.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import constants as C
from ..clock import Clock, RealClock, iso
from ..models.rayservice import parse_serve_config, statuses_for_apps
from ..util import deep_copy, resource_quantity, utc_timestamp


@dataclass
class NodeRecord:
    pod_name: str
    cluster: str
    namespace: str
    node_type: str
    group: str
    cpu: float
    memory: float
    gpu: float
    labels: dict[str, str] = field(default_factory=dict)
    resources: dict[str, float] = field(default_factory=dict)
    registered_at: str = ""
    idle_since: str = ""
    busy_until: str = ""
    alive: bool = True

    def to_dict(self) -> dict:
        return {
            "podName": self.pod_name,
            "nodeType": self.node_type,
            "group": self.group,
            "cpu": self.cpu,
            "memory": self.memory,
            "gpu": self.gpu,
            "labels": dict(self.labels),
            "resources": dict(self.resources),
            "registeredAt": self.registered_at,
            "idleSince": self.idle_since,
            "busyUntil": self.busy_until,
            "alive": self.alive,
        }


@dataclass
class JobRecord:
    job_id: str
    cluster: str
    namespace: str
    entrypoint: str
    runtime_env: str = ""
    status: str = C.JOB_STATUS_PENDING
    submitted_at: str = ""
    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: float = C.DEFAULT_JOB_DURATION_SECONDS
    exit_code: int = 0
    message: str = ""
    submission_id: str = ""
    logs: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "jobId": self.job_id,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "entrypoint": self.entrypoint,
            "runtimeEnv": self.runtime_env,
            "status": self.status,
            "submittedAt": self.submitted_at,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "durationSeconds": self.duration_seconds,
            "exitCode": self.exit_code,
            "message": self.message,
            "submissionId": self.submission_id,
            "logs": list(self.logs),
            "metadata": dict(self.metadata),
        }


@dataclass
class ServeState:
    config: str
    deployed_at: str
    deploy_seconds: float
    target_capacity: int = 100
    traffic_routed_percent: int = 100
    statuses: dict[str, dict] = field(default_factory=dict)


class ClusterRuntime:
    def __init__(self, cluster: str, namespace: str):
        self.cluster = cluster
        self.namespace = namespace
        self.nodes: dict[str, NodeRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.demand: list[dict] = []
        self.serve: ServeState | None = None
        self.removed_at: str | None = None


class RayRuntime:
    def __init__(self, clock: Clock | None = None):
        self.clock = clock or RealClock()
        self._lock = threading.RLock()
        self._clusters: dict[tuple[str, str], ClusterRuntime] = {}

    # ------------------------------------------------------------------ helpers
    def _cluster(self, namespace: str, cluster: str, create: bool = True) -> ClusterRuntime:
        key = (namespace, cluster)
        if key not in self._clusters:
            if not create:
                raise KeyError(f"cluster runtime not found: {namespace}/{cluster}")
            self._clusters[key] = ClusterRuntime(cluster, namespace)
        return self._clusters[key]

    def _seconds_since(self, timestamp: str | None) -> float:
        if not timestamp:
            return 0.0
        from ..clock import parse_iso

        return max(0.0, (self.clock.now() - parse_iso(timestamp)).total_seconds())

    def now_iso(self) -> str:
        return iso(self.clock.now())

    # -------------------------------------------------------------------- nodes
    def register_pod(self, pod: dict) -> NodeRecord:
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels") or {}
        cluster = labels.get(C.LABEL_CLUSTER, "")
        namespace = metadata.get("namespace", "default")
        if not cluster:
            raise ValueError("pod is missing the ray.io/cluster label")
        node_type = labels.get(C.LABEL_NODE_TYPE, C.NODE_TYPE_WORKER)
        group = labels.get(C.LABEL_GROUP, C.NODE_TYPE_HEAD)
        ray = None
        for item in (pod.get("spec") or {}).get("containers") or []:
            if item.get("name") == "ray":
                ray = item
                break
        requests = ((ray or {}).get("resources") or {}).get("requests") or {}
        with self._lock:
            runtime = self._cluster(namespace, cluster)
            now = self.now_iso()
            record = NodeRecord(
                pod_name=metadata.get("name", ""),
                cluster=cluster,
                namespace=namespace,
                node_type=node_type,
                group=group,
                cpu=resource_quantity(requests, "cpu"),
                memory=resource_quantity(requests, "memory"),
                gpu=resource_quantity(requests, "nvidia.com/gpu"),
                labels={key: value for key, value in labels.items() if not key.startswith("pod-template")},
                resources={
                    key: resource_quantity(requests, key)
                    for key in requests
                },
                registered_at=now,
                idle_since=now,
                alive=True,
            )
            runtime.nodes[record.pod_name] = record
            return record

    def update_pod_resources(self, pod: dict) -> NodeRecord | None:
        """Apply an in-place resize to the Ray node's logical resources.

        KubeRay's IPPR completes in two halves: the kubelet changes the cgroup
        limits and, once the resize succeeds, the autoscaler tells the Raylet
        its new capacity.  MiniKubeRay updates the single ``NodeRecord`` that
        backs both views.
        """
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels") or {}
        cluster = labels.get(C.LABEL_CLUSTER, "")
        namespace = metadata.get("namespace", "default")
        if not cluster:
            return None
        ray = None
        for item in (pod.get("spec") or {}).get("containers") or []:
            if item.get("name") == "ray":
                ray = item
                break
        requests = ((ray or {}).get("resources") or {}).get("requests") or {}
        with self._lock:
            if (namespace, cluster) not in self._clusters:
                return None
            runtime = self._clusters[(namespace, cluster)]
            record = runtime.nodes.get(metadata.get("name", ""))
            if not record:
                return None
            record.cpu = resource_quantity(requests, "cpu")
            record.memory = resource_quantity(requests, "memory")
            record.gpu = resource_quantity(requests, "nvidia.com/gpu")
            record.resources = {key: resource_quantity(requests, key) for key in requests}
            return record

    def unregister_pod(self, namespace: str, cluster: str, pod_name: str) -> None:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False) if (namespace, cluster) in self._clusters else None
            if runtime and pod_name in runtime.nodes:
                runtime.nodes[pod_name].alive = False
                del runtime.nodes[pod_name]

    def nodes(self, namespace: str, cluster: str) -> list[NodeRecord]:
        with self._lock:
            if (namespace, cluster) not in self._clusters:
                return []
            return list(self._clusters[(namespace, cluster)].nodes.values())

    def ready_nodes(self, namespace: str, cluster: str) -> list[NodeRecord]:
        return [node for node in self.nodes(namespace, cluster) if node.alive]

    def mark_busy(self, namespace: str, cluster: str, pod_name: str | None = None, seconds: float = 1.0) -> None:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False)
            nodes = [runtime.nodes[pod_name]] if pod_name and pod_name in runtime.nodes else list(runtime.nodes.values())
            now = self.clock.now()
            from datetime import timedelta

            for node in nodes:
                node.busy_until = iso(now + timedelta(seconds=seconds))
                node.idle_since = ""

    def node_is_idle(self, node: NodeRecord, idle_timeout: float) -> bool:
        from ..clock import parse_iso

        now = self.clock.now()
        if node.busy_until and now < parse_iso(node.busy_until):
            return False
        reference = node.busy_until or node.idle_since or node.registered_at
        if not reference:
            return False
        return (now - parse_iso(reference)).total_seconds() >= idle_timeout

    def mark_idle(self, node: NodeRecord) -> None:
        now = self.now_iso()
        node.idle_since = now
        node.busy_until = ""

    # ------------------------------------------------------------------- demand
    def add_demand(
        self,
        namespace: str,
        cluster: str,
        *,
        cpu: float = 0.0,
        gpu: float = 0.0,
        memory_mb: float = 0.0,
        count: int = 1,
        ttl_seconds: float = 30.0,
        label_selector: dict | None = None,
    ) -> None:
        """Queue pending work.

        ``cpu`` is expressed in cores for a friendly API and converted to
        millicores internally, matching Pod resource requests.  ``memory_mb``
        is MiB and is used by IPPR to decide whether a Pod can grow vertically.
        """
        with self._lock:
            runtime = self._cluster(namespace, cluster)
            runtime.demand.append(
                {
                    "cpu": float(cpu) * 1000.0,
                    "gpu": float(gpu),
                    "memory": float(memory_mb) * 1024.0 * 1024.0,
                    "count": count,
                    "expiresAt": self.now_iso() if ttl_seconds <= 0 else _add_seconds(self.clock.now(), ttl_seconds),
                    "labelSelector": dict(label_selector or {}),
                }
            )

    def pending_demand(self, namespace: str, cluster: str) -> dict[str, float]:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False) if (namespace, cluster) in self._clusters else None
            if not runtime:
                return {"cpu": 0.0, "gpu": 0.0, "memory": 0.0, "count": 0.0}
            now = self.clock.now()
            from ..clock import parse_iso

            runtime.demand = [item for item in runtime.demand if parse_iso(item["expiresAt"]) > now]
            return {
                "cpu": sum(item["cpu"] * item["count"] for item in runtime.demand),
                "gpu": sum(item["gpu"] * item["count"] for item in runtime.demand),
                "memory": sum(item.get("memory", 0.0) * item["count"] for item in runtime.demand),
                "count": float(sum(item["count"] for item in runtime.demand)),
            }

    def clear_demand(self, namespace: str, cluster: str) -> None:
        with self._lock:
            self._cluster(namespace, cluster).demand.clear()

    # --------------------------------------------------------------------- jobs
    def submit_job(
        self,
        namespace: str,
        cluster: str,
        job_id: str,
        entrypoint: str,
        *,
        runtime_env: str = "",
        duration_seconds: float | None = None,
        exit_code: int = 0,
        metadata: dict | None = None,
        submission_id: str = "",
    ) -> JobRecord:
        with self._lock:
            runtime = self._cluster(namespace, cluster)
            if job_id in runtime.jobs and runtime.jobs[job_id].status not in C.TERMINAL_JOB_STATUSES:
                return runtime.jobs[job_id]
            now = self.now_iso()
            record = JobRecord(
                job_id=job_id,
                cluster=cluster,
                namespace=namespace,
                entrypoint=entrypoint,
                runtime_env=runtime_env,
                status=C.JOB_STATUS_PENDING,
                submitted_at=now,
                duration_seconds=duration_seconds if duration_seconds is not None else C.DEFAULT_JOB_DURATION_SECONDS,
                exit_code=exit_code,
                submission_id=submission_id or job_id,
                metadata=dict(metadata or {}),
            )
            record.logs.append(f"{now} [ray] submitting job {job_id} to {cluster}")
            record.logs.append(f"{now} [ray] entrypoint: {entrypoint}")
            runtime.jobs[job_id] = record
            return record

    def refresh_job(self, namespace: str, cluster: str, job_id: str) -> JobRecord | None:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False) if (namespace, cluster) in self._clusters else None
            if not runtime or job_id not in runtime.jobs:
                return None
            job = runtime.jobs[job_id]
            if job.status in C.TERMINAL_JOB_STATUSES:
                return job
            elapsed = self._seconds_since(job.submitted_at)
            if job.status == C.JOB_STATUS_PENDING:
                # A pending Ray job starts as soon as the head GCS is reachable.
                head_alive = any(node.node_type == C.NODE_TYPE_HEAD and node.alive for node in runtime.nodes.values())
                if head_alive or elapsed >= 0.05:
                    job.status = C.JOB_STATUS_RUNNING
                    job.start_time = iso(self.clock.now())
                    job.logs.append(f"{job.start_time} [ray] job {job_id} is RUNNING")
                    self.mark_busy(namespace, cluster, seconds=job.duration_seconds)
            if job.status == C.JOB_STATUS_RUNNING:
                running_for = self._seconds_since(job.start_time)
                if running_for >= job.duration_seconds:
                    job.end_time = iso(self.clock.now())
                    if job.exit_code == 0:
                        job.status = C.JOB_STATUS_SUCCEEDED
                        job.logs.append(f"{job.end_time} [ray] job {job_id} SUCCEEDED")
                    else:
                        job.status = C.JOB_STATUS_FAILED
                        job.message = f"entrypoint exited with code {job.exit_code}"
                        job.logs.append(f"{job.end_time} [ray] job {job_id} FAILED: exit code {job.exit_code}")
            return job

    def get_job(self, namespace: str, cluster: str, job_id: str) -> JobRecord | None:
        return self.refresh_job(namespace, cluster, job_id)

    def stop_job(self, namespace: str, cluster: str, job_id: str) -> bool:
        with self._lock:
            job = self.refresh_job(namespace, cluster, job_id)
            if not job:
                return False
            if job.status not in C.TERMINAL_JOB_STATUSES:
                job.status = C.JOB_STATUS_STOPPED
                job.end_time = iso(self.clock.now())
                job.message = "stopped by user"
                job.logs.append(f"{job.end_time} [ray] job {job_id} STOPPED")
            return True

    def job_logs(self, namespace: str, cluster: str, job_id: str) -> list[str]:
        job = self.refresh_job(namespace, cluster, job_id)
        return list(job.logs) if job else []

    def list_jobs(self, namespace: str, cluster: str) -> list[JobRecord]:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False) if (namespace, cluster) in self._clusters else None
            if not runtime:
                return []
            for job_id in list(runtime.jobs):
                self.refresh_job(namespace, cluster, job_id)
            return list(runtime.jobs.values())

    # -------------------------------------------------------------------- serve
    def deploy_serve(self, namespace: str, cluster: str, config: str, *, deploy_seconds: float | None = None, target_capacity: int = 100) -> ServeState:
        with self._lock:
            runtime = self._cluster(namespace, cluster)
            if runtime.serve and runtime.serve.config == config:
                return runtime.serve
            parsed = parse_serve_config(config)
            state = ServeState(
                config=config,
                deployed_at=self.now_iso(),
                deploy_seconds=deploy_seconds if deploy_seconds is not None else C.DEFAULT_SERVE_DEPLOY_SECONDS,
                target_capacity=target_capacity,
                statuses=statuses_for_apps(config, ready=False, message="deploying"),
            )
            runtime.serve = state
            _ = parsed
            return state

    def serve_status(self, namespace: str, cluster: str) -> tuple[bool, dict[str, dict]]:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False) if (namespace, cluster) in self._clusters else None
            if not runtime or not runtime.serve:
                return False, {}
            state = runtime.serve
            ready = self._seconds_since(state.deployed_at) >= state.deploy_seconds
            state.statuses = statuses_for_apps(state.config, ready=ready, message="" if ready else "deploying")
            return ready, deep_copy(state.statuses)

    def set_serve_traffic(self, namespace: str, cluster: str, percent: int) -> None:
        with self._lock:
            runtime = self._cluster(namespace, cluster, create=False) if (namespace, cluster) in self._clusters else None
            if runtime and runtime.serve:
                runtime.serve.traffic_routed_percent = max(0, min(100, percent))

    def serve_traffic(self, namespace: str, cluster: str) -> int:
        with self._lock:
            runtime = self._clusters.get((namespace, cluster))
            return runtime.serve.traffic_routed_percent if runtime and runtime.serve else 0

    def set_target_capacity(self, namespace: str, cluster: str, percent: int) -> None:
        with self._lock:
            runtime = self._clusters.get((namespace, cluster))
            if runtime and runtime.serve:
                runtime.serve.target_capacity = max(0, min(100, percent))

    # ---------------------------------------------------------------- lifecycle
    def remove_cluster(self, namespace: str, cluster: str) -> None:
        with self._lock:
            runtime = self._clusters.pop((namespace, cluster), None)
            if runtime:
                runtime.removed_at = self.now_iso()
                for node in runtime.nodes.values():
                    node.alive = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "clusters": {
                    f"{namespace}/{cluster}": {
                        "nodes": [node.to_dict() for node in runtime.nodes.values()],
                        "jobs": [job.to_dict() for job in runtime.jobs.values()],
                        "demand": deep_copy(runtime.demand),
                        "serve": {
                            "deployedAt": runtime.serve.deployed_at,
                            "targetCapacity": runtime.serve.target_capacity,
                            "trafficRoutedPercent": runtime.serve.traffic_routed_percent,
                            "statuses": deep_copy(runtime.serve.statuses),
                        }
                        if runtime.serve
                        else None,
                    }
                    for (namespace, cluster), runtime in self._clusters.items()
                }
            }


def _add_seconds(dt: datetime, seconds: float) -> str:
    from datetime import timedelta

    return iso(dt + timedelta(seconds=seconds))
