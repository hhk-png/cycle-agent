"""End-to-end demonstration used by ``minikuberay demo`` and the tutorials."""

from __future__ import annotations

from . import constants as C
from .clock import ManualClock
from .controllers.base import Operator, OperatorConfig
from .models.meta import new_object
from .version import KUBERAY_VERSION, __version__


def _cluster_spec(*, workers: int = 2, autoscaling: bool = False) -> dict:
    spec = {
        "rayVersion": "2.59.0",
        "headGroupSpec": {
            "rayStartParams": {},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "ray",
                            "image": C.DEFAULT_IMAGE,
                            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
                        }
                    ]
                }
            },
        },
        "workerGroupSpecs": [
            {
                "groupName": "workers",
                "replicas": workers,
                "minReplicas": 0 if autoscaling else workers,
                "maxReplicas": 6,
                "rayStartParams": {},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "ray",
                                "image": C.DEFAULT_IMAGE,
                                "resources": {"requests": {"cpu": "2", "memory": "2Gi"}},
                            }
                        ]
                    }
                },
            }
        ],
    }
    if autoscaling:
        spec["enableInTreeAutoscaling"] = True
        spec["autoscalerOptions"] = {"idleTimeoutSeconds": 15, "upscalingMode": "Default"}
    return spec


def _serve_config(version: str) -> str:
    return f"""applications:
  - name: demo
    import_path: demo_app:{version}
    route_prefix: /
    deployments:
      - name: DemoDeployment
        num_replicas: 2
"""


def run_demo(*, fast: bool = False, verbose: bool = True, namespace: str = "default", execute: bool = False) -> int:
    clock = ManualClock()
    config = OperatorConfig(namespace=None)
    config.feature_gates["RayCronJob"] = True
    operator = Operator(clock=clock, config=config)
    if execute:
        print("note: --execute is accepted for interface parity; MiniKubeRay's demo runtime is simulated.")
    if verbose:
        print("=" * 72)
        print(f"MiniKubeRay {__version__} - upstream API {KUBERAY_VERSION}")
        print("=" * 72)

    def step(count: int = 1, advance: float = 0.2) -> None:
        for _ in range(count):
            operator.run_once()
            clock.advance(advance)

    # ---------------------------------------------------------------- RayCluster
    cluster = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCLUSTER,
        "demo-cluster",
        namespace,
        spec=_cluster_spec(workers=2),
    )
    operator.store.create(C.KIND_RAYCLUSTER, cluster)
    step(12)
    cluster_status = operator.store.get(C.KIND_RAYCLUSTER, "demo-cluster", namespace)["status"]
    if verbose:
        print("\n[1] RayCluster")
        print(f"    state={cluster_status.get('state')} workers={cluster_status.get('readyWorkerReplicas')}/{cluster_status.get('desiredWorkerReplicas')}")
        print(f"    dashboard={cluster_status.get('endpoints', {}).get('dashboard')}")
        print(f"    head={cluster_status.get('head', {}).get('podName')} podIP={cluster_status.get('head', {}).get('podIP')}")

    # -------------------------------------------------------------------- RayJob
    rayjob = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYJOB,
        "demo-job",
        namespace,
        spec={
            "entrypoint": "python -c \"print('hello from Ray')\"",
            "submissionMode": "K8sJobMode",
            "shutdownAfterJobFinishes": True,
            "ttlSecondsAfterFinished": 0,
            "backoffLimit": 1,
            "rayClusterSpec": _cluster_spec(workers=0),
        },
    )
    operator.store.create(C.KIND_RAYJOB, rayjob)
    step(18, advance=0.5)
    job_status = operator.store.get(C.KIND_RAYJOB, "demo-job", namespace)["status"]
    if verbose:
        print("\n[2] RayJob")
        print(f"    deployment={job_status.get('jobDeploymentStatus')} job={job_status.get('jobStatus')} id={job_status.get('jobId')}")
        print(f"    cluster={job_status.get('rayClusterName')} submitter cleaned up={not operator.store.list(C.KIND_JOB, namespace, label_selector={'job-name': 'demo-job-submitter'})}")

    # --------------------------------------------------------------- autoscaling
    auto = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCLUSTER,
        "demo-autoscaled",
        namespace,
        spec=_cluster_spec(workers=0, autoscaling=True),
    )
    operator.store.create(C.KIND_RAYCLUSTER, auto)
    step(10)
    operator.runtime.add_demand(namespace, "demo-autoscaled", cpu=6, count=1, ttl_seconds=120)
    step(12)
    scaled = operator.store.get(C.KIND_RAYCLUSTER, "demo-autoscaled", namespace)
    replicas_after_up = scaled["spec"]["workerGroupSpecs"][0]["replicas"]
    operator.runtime.clear_demand(namespace, "demo-autoscaled")
    step(90, advance=1.0)
    scaled = operator.store.get(C.KIND_RAYCLUSTER, "demo-autoscaled", namespace)
    replicas_after_down = scaled["spec"]["workerGroupSpecs"][0]["replicas"]
    if verbose:
        print("\n[3] In-tree autoscaling")
        print(f"    pending cpu=6 cores -> replicas={replicas_after_up}")
        print(f"    idle for 15s      -> replicas={replicas_after_down}")

    # ----------------------------------------------------------------- RayService
    service = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYSERVICE,
        "demo-service",
        namespace,
        spec={
            "serveConfigV2": _serve_config("v1"),
            "rayClusterConfig": _cluster_spec(workers=1),
        },
    )
    operator.store.create(C.KIND_RAYSERVICE, service)
    step(14)
    service_status = operator.store.get(C.KIND_RAYSERVICE, "demo-service", namespace)["status"]
    if verbose:
        print("\n[4] RayService")
        print(f"    status={service_status.get('serviceStatus')} endpoints={service_status.get('numServeEndpoints')}")
        print(f"    active={service_status.get('activeServiceStatus', {}).get('rayClusterName')} apps={list(service_status.get('activeServiceStatus', {}).get('applicationStatuses', {}))}")
    service = operator.store.get(C.KIND_RAYSERVICE, "demo-service", namespace)
    service["spec"]["serveConfigV2"] = _serve_config("v2")
    operator.store.update(C.KIND_RAYSERVICE, service)
    step(20)
    service_status = operator.store.get(C.KIND_RAYSERVICE, "demo-service", namespace)["status"]
    if verbose:
        print(f"    upgraded active={service_status.get('activeServiceStatus', {}).get('rayClusterName')} status={service_status.get('serviceStatus')}")

    # ----------------------------------------------------------------- RayCronJob
    cron = new_object(
        C.RAY_API_VERSION,
        C.KIND_RAYCRONJOB,
        "demo-cron",
        namespace,
        spec={
            "schedule": "*/5 * * * *",
            "jobTemplate": {
                "entrypoint": "python -c \"print('scheduled')\"",
                "shutdownAfterJobFinishes": True,
                "rayClusterSpec": _cluster_spec(workers=0),
            },
        },
    )
    operator.store.create(C.KIND_RAYCRONJOB, cron)
    step(7, advance=60)
    cron_status = operator.store.get(C.KIND_RAYCRONJOB, "demo-cron", namespace).get("status", {})
    scheduled_jobs = operator.store.list(C.KIND_RAYJOB, namespace, label_selector={C.LABEL_CRONJOB_NAME: "demo-cron"})
    if verbose:
        print("\n[5] RayCronJob")
        print(f"    lastScheduleTime={cron_status.get('lastScheduleTime')} created RayJobs={len(scheduled_jobs)}")
        print(f"    child name={scheduled_jobs[0]['metadata']['name'] if scheduled_jobs else '-'}")

    if verbose:
        print("\n[6] Metrics (selected)")
        for line in operator.metrics.registry.render().splitlines():
            if any(key in line for key in ("raycluster_ready", "rayjob_status", "rayservice_ready", "autoscaler_actions_total")):
                print("    " + line)
        print("\nDemo complete. All state was produced by the same reconcilers the tutorials walk through.")
    return 0
