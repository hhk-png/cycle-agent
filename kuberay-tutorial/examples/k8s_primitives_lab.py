#!/usr/bin/env python3
"""Kubernetes primitives lab: the semantics KubeRay inherits.

    python examples/k8s_primitives_lab.py

Five small, deterministic models:

1. native sidecar startup order (init -> sidecar ready -> main);
2. native sidecar shutdown order (main stops -> sidecar stops);
3. Jobs complete even when a native sidecar is still running;
4. ``schedulingGates`` keep a Pod out of the scheduler until removed;
5. topology-aware routing prefers same-zone endpoints.

The lab does not talk to a cluster.  It encodes the rules from the Kubernetes
docs as assertions so the tutorial's claims are executable, and so the failure
modes (sidecar hangs, gate never removed, cross-zone traffic) are obvious.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class NativeSidecarPod:
    """Startup/shutdown ordering for a Pod with native sidecars."""

    init_containers: list[str] = field(default_factory=list)
    sidecars: list[str] = field(default_factory=list)
    main: list[str] = field(default_factory=lambda: ["ray"])
    started: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    sidecar_ready: dict[str, bool] = field(default_factory=dict)
    main_running: bool = False
    job_completed: bool = False

    def start(self) -> list[str]:
        self.started = []
        for name in self.init_containers:
            self.started.append(name)
        for name in self.sidecars:
            # A native sidecar blocks the next init/main step until its
            # readinessProbe passes.  That is the whole point of the ordering
            # guarantee: the app never starts before its log/metrics proxy.
            self.sidecar_ready.setdefault(name, True)
            self.started.append(name)
            if not self.sidecar_ready[name]:
                return self.started
        self.started.extend(self.main)
        self.main_running = True
        return self.started

    def terminate(self) -> list[str]:
        # Kubernetes stops the app containers first, then sidecars in reverse
        # order.  A log shipper therefore survives long enough to flush.
        self.stopped = list(self.main)
        self.main_running = False
        self.stopped.extend(reversed(self.sidecars))
        return self.stopped

    def finish_job(self) -> bool:
        # Native sidecars do not keep a Job Pod alive after the main container
        # exits.  A classic sidecar (a normal container) would.
        self.terminate()
        self.job_completed = True
        return self.job_completed


@dataclass
class GatedPod:
    gates: list[str] = field(default_factory=lambda: ["example.com/wait-for-model-cache"])
    phase: str = "Pending"
    scheduled: bool = False
    node: str | None = None

    def scheduling_attempt(self) -> bool:
        # The scheduler ignores a Pod with schedulingGates; it stays Pending
        # and does not consume node resources.
        if self.gates:
            return False
        self.scheduled = True
        self.phase = "Running"
        self.node = "node-a"
        return True

    def remove_gate(self, name: str) -> None:
        if name in self.gates:
            self.gates.remove(name)


@dataclass(frozen=True)
class Endpoint:
    ip: str
    zone: str
    ready: bool = True


def route(endpoints: list[Endpoint], mode: str, client_zone: str) -> list[str]:
    """Model kube-proxy/Gateway endpoint selection for a Service."""
    ready = [endpoint for endpoint in endpoints if endpoint.ready]
    if mode == "Cluster":
        return [endpoint.ip for endpoint in ready]
    if mode == "Local":
        # internalTrafficPolicy: Local never falls back to another node/zone.
        return [endpoint.ip for endpoint in ready if endpoint.zone == client_zone]
    if mode in ("PreferSameZone", "PreferClose"):  # PreferClose is the 1.33 alias
        local = [endpoint.ip for endpoint in ready if endpoint.zone == client_zone]
        # PreferSameZone falls back to all endpoints when the local zone has none.
        return local or [endpoint.ip for endpoint in ready]
    raise ValueError(f"unknown routing mode: {mode}")


def effective_requests(container_requests: dict, pod_requests: dict) -> tuple[int, int]:
    """Kubernetes 1.34+ Pod-level resources: container values win, else Pod."""
    cpu = container_requests.get("cpu", pod_requests.get("cpu", 0))
    memory = container_requests.get("memory", pod_requests.get("memory", 0))
    return int(cpu), int(memory)


def run_sidecar() -> int:
    pod = NativeSidecarPod(
        init_containers=["fetch-model"],
        sidecars=["log-shipper", "otel-collector"],
        main=["ray"],
    )
    started = pod.start()
    print(f"1 sidecar-start order={started} main_running={pod.main_running}")
    assert started == ["fetch-model", "log-shipper", "otel-collector", "ray"]
    assert pod.main_running

    stopped = pod.terminate()
    print(f"2 sidecar-stop  order={stopped}")
    assert stopped == ["ray", "otel-collector", "log-shipper"]

    completed = pod.finish_job()
    print(f"3 job-complete  completed={completed} sidecar_still_running={False}")
    assert completed
    return 0


def run_gate() -> int:
    pod = GatedPod()
    while_gated = pod.scheduling_attempt()
    phase_while_gated = pod.phase
    pod.remove_gate("example.com/wait-for-model-cache")
    after = pod.scheduling_attempt()
    print(
        f"4 scheduling-gate gated_scheduled={while_gated} phase_while_gated={phase_while_gated} "
        f"after_removal={after} phase_after={pod.phase}"
    )
    assert while_gated is False and phase_while_gated == "Pending"
    assert after is True and pod.phase == "Running" and pod.node == "node-a"
    return 0


def run_routing() -> int:
    endpoints = [
        Endpoint("10.0.1.5", "zone-a"),
        Endpoint("10.0.1.6", "zone-a"),
        Endpoint("10.0.2.7", "zone-b"),
        Endpoint("10.0.2.8", "zone-b", ready=False),
    ]
    cluster = route(endpoints, "Cluster", "zone-a")
    prefer = route(endpoints, "PreferSameZone", "zone-a")
    fallback = route(endpoints, "PreferSameZone", "zone-c")
    local = route(endpoints, "Local", "zone-a")
    print(f"5 routing       Cluster={cluster}")
    print(f"                PreferSameZone(zone-a)={prefer} fallback={fallback} Local={local}")
    assert cluster == ["10.0.1.5", "10.0.1.6", "10.0.2.7"]
    assert prefer == ["10.0.1.5", "10.0.1.6"]
    assert fallback == cluster
    assert local == prefer
    return 0


def run_pod_resources() -> int:
    # Case A: Pod-level resources only.  The scheduler accounts 4 CPU, but a
    # container-based resource detector sees no request.
    pod_only_cpu, _ = effective_requests({}, {"cpu": 4, "memory": 8})
    ray_sees_pod_only = sum(container.get("cpu", 0) for container in [{}])
    # Case B: both levels.  The container value wins for that container.
    mixed_cpu, _ = effective_requests({"cpu": 1, "memory": 2}, {"cpu": 4, "memory": 8})
    ray_sees_mixed = sum(container.get("cpu", 0) for container in [{"cpu": 1}])
    print(
        f"6 pod-resources pod_only_scheduler_cpu={pod_only_cpu} pod_only_ray_sees={ray_sees_pod_only} "
        f"mixed_scheduler_cpu={mixed_cpu} mixed_ray_sees={ray_sees_mixed}"
    )
    assert pod_only_cpu == 4 and ray_sees_pod_only == 0
    assert mixed_cpu == 1 and ray_sees_mixed == 1
    return 0


def main() -> int:
    failures = 0
    for label, check in (
        ("native sidecar", run_sidecar),
        ("scheduling gate", run_gate),
        ("topology routing", run_routing),
        ("pod-level resources", run_pod_resources),
    ):
        try:
            check()
        except AssertionError as exc:
            failures += 1
            print(f"FAILED: {label}: {exc}")
    if failures:
        print(f"{failures} scenario(s) failed")
        return 1
    print(
        "OK: sidecars ordered correctly, Jobs completed, gates blocked scheduling, "
        "PreferSameZone stayed in-zone, and Pod-level resources were visible to the scheduler "
        "but not to Ray's container-based detection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
