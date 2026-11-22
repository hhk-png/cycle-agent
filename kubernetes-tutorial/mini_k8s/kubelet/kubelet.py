"""A simulated kubelet: pod lifecycle, probes, restarts, logs and exec.

No real container is started.  The runtime state machine mirrors the observable
Kubernetes contract (Pod phases, container states, readiness, restart counts),
which is what the tutorial's tests and examples exercise.  The module boundary
is where a real CRI client would live.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mini_k8s.apimachinery.meta import deep_copy, now_iso
from mini_k8s.apimachinery.quantity import parse_quantity

POD_GVR = ("", "v1", "pods")
NODE_GVR = ("", "v1", "nodes")
EVENT_GVR = ("", "v1", "events")
CONFIGMAP_GVR = ("", "v1", "configmaps")
SECRET_GVR = ("", "v1", "secrets")
PVC_GVR = ("", "v1", "persistentvolumeclaims")


@dataclass
class ContainerState:
    name: str
    requested_ticks: int | None = None
    ticks: int = 0
    started: bool = False
    ready: bool = False
    terminated: bool = False
    exit_code: int = 0
    restart_count: int = 0
    started_at: str = now_iso()
    terminated_at: str | None = None
    liveness_failures: int = 0
    readiness_failures: int = 0
    startup_ok: bool = False
    startup_failures: int = 0
    last_state: dict = field(default_factory=dict)
    logs: deque[str] = field(default_factory=deque)
    previous_logs: deque[str] = field(default_factory=deque)
    ready_after: int = 0
    probe_ok: bool = True


@dataclass
class PodRuntime:
    uid: str
    namespace: str
    name: str
    node_name: str
    pod_ip: str
    start_time: str = now_iso()
    init_started: bool = False
    init_done: bool = False
    containers: dict[str, ContainerState] = field(default_factory=dict)
    phase: str = "Pending"
    last_message: str = ""
    deleted: bool = False
    initialized_event: bool = False


class Kubelet:
    def __init__(self, store, node_name: str, *, pod_cidr: str = "10.244.1.0/24",
                 cpu: str = "4", memory: str = "8Gi", pods: int = 110,
                 tick_interval: float = 0.1, heartbeat_interval: float = 0.5,
                 extended_resources: dict[str, str] | None = None):
        self.store = store
        self.node_name = node_name
        self.pod_cidr = pod_cidr
        # Extended resources (for example nvidia.com/gpu) are advertised just
        # like cpu/memory.  A real kubelet learns them from device plugins via
        # ListAndWatch; MiniK8s takes them as static node capacity.
        self.capacity = {"cpu": cpu, "memory": memory, "pods": str(pods), "ephemeral-storage": "100Gi"}
        self.capacity.update(extended_resources or {})
        self.tick_interval = tick_interval
        self.heartbeat_interval = heartbeat_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.runtimes: dict[str, PodRuntime] = {}
        self._ip_counter = 2
        self._last_heartbeat = 0.0
        self._lock = threading.RLock()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> "Kubelet":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mini-k8s-kubelet-{self.node_name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                self.reconcile()
                if now - self._last_heartbeat >= self.heartbeat_interval:
                    self.update_node_status()
                    self._last_heartbeat = now
            except Exception as exc:
                self._event("Warning", "KubeletError", str(exc), pod=None)
            self._stop.wait(self.tick_interval)

    # ------------------------------------------------------------------- pod loop
    def reconcile(self) -> None:
        with self._lock:
            pods = [p for p in self.store.list(POD_GVR, None)[0]
                    if p.get("spec", {}).get("nodeName") == self.node_name]
            existing_uids = set()
            active_uids = set()
            for pod in pods:
                uid = pod["metadata"].get("uid")
                existing_uids.add(uid)
                if pod["metadata"].get("deletionTimestamp") or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                    # Keep terminal pods visible (logs/tests) but stop advancing them.
                    if uid in self.runtimes:
                        self.runtimes[uid].deleted = bool(pod["metadata"].get("deletionTimestamp"))
                    continue
                active_uids.add(uid)
                self._reconcile_pod(pod)
            for uid in list(self.runtimes):
                if uid not in existing_uids:
                    # The Pod object is gone; drop its runtime (and logs).
                    self.runtimes.pop(uid, None)
                    continue
                if uid in active_uids:
                    continue
                runtime = self.runtimes[uid]
                if runtime.phase in ("Succeeded", "Failed"):
                    # Keep terminal runtimes while the Pod object exists so
                    # logs remain readable, matching kubelet log retention.
                    continue
                # The Pod was moved or is terminating; stop its runtime.
                self.runtimes.pop(uid, None)

    def _reconcile_pod(self, pod: dict) -> None:
        uid = pod["metadata"]["uid"]
        namespace = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        spec = pod.get("spec", {})
        runtime = self.runtimes.get(uid)
        if runtime is None:
            runtime = PodRuntime(uid=uid, namespace=namespace, name=name, node_name=self.node_name,
                                 pod_ip=self._allocate_ip())
            self.runtimes[uid] = runtime
            self._event("Normal", "Pulling", f"Pulling image for pod {namespace}/{name}", pod)
        self._ensure_volumes(pod)
        missing = self._missing_volumes(pod)
        if missing:
            runtime.phase = "Pending"
            runtime.last_message = f"container runtime: waiting for volumes: {', '.join(missing)}"
            self._write_status(pod, runtime)
            return
        init_containers = spec.get("initContainers", [])
        if not runtime.init_done:
            self._run_init_containers(pod, runtime, init_containers)
            if not runtime.init_done:
                self._write_status(pod, runtime)
                return
        for container in spec.get("containers", []):
            name_c = container.get("name", "")
            state = runtime.containers.get(name_c)
            if state is None:
                state = ContainerState(name=name_c)
                state.requested_ticks = self._run_ticks(pod, container)
                state.ready_after = self._ready_after(pod, container)
                runtime.containers[name_c] = state
                self._append_log(state, f"Starting container {name_c} with image {container.get('image')}")
                self._event("Normal", "Started", f"Started container {name_c}", pod)
            self._advance_container(pod, runtime, container, state)
        self._derive_phase(runtime, spec)
        self._write_status(pod, runtime)

    def _run_init_containers(self, pod: dict, runtime: PodRuntime, init_containers: list[dict]) -> None:
        if not init_containers:
            runtime.init_done = True
            return
        if not runtime.init_started:
            runtime.init_started = True
            self._event("Normal", "Started", "Started init containers", pod)
        runtime.init_done = True
        for index, container in enumerate(init_containers):
            state = runtime.containers.setdefault(f"init:{container['name']}", ContainerState(name=f"init:{container['name']}"))
            state.started = True
            state.ticks += 1
            state.terminated = True
            state.exit_code = 0
            state.ready = False
            state.logs.append(f"init container {container['name']} completed")
        runtime.initialized_event = True

    def _advance_container(self, pod: dict, runtime: PodRuntime, container: dict, state: ContainerState) -> None:
        if state.terminated:
            restart_policy = pod.get("spec", {}).get("restartPolicy", "Always")
            if restart_policy == "Always" or (restart_policy == "OnFailure" and state.exit_code != 0):
                state.last_state = {"terminated": {
                    "exitCode": state.exit_code,
                    "reason": "Completed" if state.exit_code == 0 else "Error",
                    "startedAt": state.started_at,
                    "finishedAt": state.terminated_at or now_iso(),
                }}
                state.previous_logs.extend(state.logs)
                state.logs.clear()
                state.terminated = False
                state.started = True
                state.ready = False
                state.restart_count += 1
                state.ticks = 0
                state.liveness_failures = 0
                state.readiness_failures = 0
                state.startup_ok = False
                state.startup_failures = 0
                state.started_at = now_iso()
                self._append_log(state, f"Restarting container {state.name} (restart count {state.restart_count})")
                container = next((c for c in pod.get("spec", {}).get("containers", []) if c.get("name") == state.name), {})
                self._append_log(state, f"Starting container {state.name} with image {container.get('image')}")
                self._event("Normal", "Started", f"Started container {state.name}", pod)
            return
        state.started = True
        state.ticks += 1
        # A startup probe gates readiness and disables liveness until it
        # succeeds.  This is what lets slow-starting applications avoid being
        # killed by a liveness probe during initialization.
        startup = container.get("startupProbe")
        if startup is not None and not state.startup_ok:
            if state.ticks >= int(startup.get("initialDelaySeconds", 0)):
                if self._probe(pod, container, startup, state):
                    state.startup_ok = True
                    state.startup_failures = 0
                else:
                    state.startup_failures += 1
                    if state.startup_failures >= int(startup.get("failureThreshold", 3)):
                        state.terminated = True
                        state.exit_code = 137
                        state.terminated_at = now_iso()
                        state.ready = False
                        self._append_log(state, "Startup probe failed; killing container")
                        self._event("Warning", "Unhealthy",
                                    f"Startup probe failed for container {state.name}; restarting", pod)
                        return
            state.probe_ok = state.startup_ok
            state.ready = state.startup_ok and state.ticks >= state.ready_after
        else:
            # Probes gate readiness and can restart a container.
            probe_ok = True
            readiness = container.get("readinessProbe")
            if readiness is not None:
                ok = self._probe(pod, container, readiness, state)
                state.readiness_failures = 0 if ok else state.readiness_failures + 1
                probe_ok = ok
            state.probe_ok = probe_ok
            ready_delay_done = state.ticks >= state.ready_after
            state.ready = (not state.terminated) and state.probe_ok and ready_delay_done
            liveness = container.get("livenessProbe")
            if liveness is not None and state.ticks >= int(liveness.get("initialDelaySeconds", 0)):
                if not self._probe(pod, container, liveness, state):
                    state.liveness_failures += 1
                    if state.liveness_failures >= int(liveness.get("failureThreshold", 3)):
                        state.terminated = True
                        state.exit_code = 137
                        state.terminated_at = now_iso()
                        state.ready = False
                        self._append_log(state, "Liveness probe failed; killing container")
                        self._event("Warning", "Unhealthy", f"Liveness probe failed for container {state.name}; restarting", pod)
                        return
                else:
                    state.liveness_failures = 0
        if state.requested_ticks is not None and state.ticks >= state.requested_ticks:
            exit_code = self._exit_code(pod, container)
            state.terminated = True
            state.exit_code = exit_code
            state.terminated_at = now_iso()
            state.ready = False
            self._append_log(state, f"Container {state.name} exited with code {exit_code}")
            if exit_code != 0:
                self._event("Warning", "Failed", f"Container {state.name} exited with code {exit_code}", pod)
            else:
                self._event("Normal", "Killing", f"Container {state.name} completed", pod)

    def _derive_phase(self, runtime: PodRuntime, spec: dict) -> None:
        if not runtime.init_done:
            runtime.phase = "Pending"
            return
        containers = [c for c in runtime.containers.values() if not c.name.startswith("init:")]
        if not containers:
            runtime.phase = "Pending"
            return
        restart_policy = spec.get("restartPolicy", "Always")
        all_terminated = all(c.terminated for c in containers)
        if all_terminated:
            if all(c.exit_code == 0 for c in containers):
                if restart_policy == "Never":
                    runtime.phase = "Succeeded"
                    return
                if restart_policy == "OnFailure":
                    runtime.phase = "Succeeded"
                    return
            else:
                if restart_policy == "Never":
                    runtime.phase = "Failed"
                    return
        if any(c.started for c in containers):
            runtime.phase = "Running"
        else:
            runtime.phase = "Pending"

    def _write_status(self, pod: dict, runtime: PodRuntime) -> None:
        containers = [c for c in runtime.containers.values() if not c.name.startswith("init:")]
        init_states = [c for c in runtime.containers.values() if c.name.startswith("init:")]
        all_ready = bool(containers) and all(c.ready for c in containers)
        ready_condition = "True" if all_ready and runtime.phase == "Running" else "False"
        if runtime.phase == "Succeeded":
            ready_condition = "False"
        conditions = [
            {"type": "PodScheduled", "status": "True", "reason": "Scheduled", "lastTransitionTime": runtime.start_time},
            {"type": "Initialized", "status": "True" if runtime.init_done else "False",
             "reason": "Initialized" if runtime.init_done else "ContainersNotInitialized",
             "lastTransitionTime": runtime.start_time},
            {"type": "ContainersReady", "status": ready_condition,
             "reason": "ContainersReady" if ready_condition == "True" else "ContainersNotReady",
             "lastTransitionTime": runtime.start_time},
            {"type": "Ready", "status": ready_condition,
             "reason": "PodReady" if ready_condition == "True" else "PodNotReady",
             "lastTransitionTime": runtime.start_time},
        ]
        status = {
            "phase": runtime.phase,
            "hostIP": "127.0.0.1",
            "podIP": runtime.pod_ip,
            "podIPs": [{"ip": runtime.pod_ip}],
            "startTime": runtime.start_time,
            "conditions": conditions,
            "containerStatuses": [self._container_status(state, pod) for state in containers],
            "initContainerStatuses": [self._container_status(state, pod) for state in init_states],
            "qosClass": _qos_class(pod.get("spec", {})),
        }
        if runtime.last_message:
            status["message"] = runtime.last_message
        current = pod.get("status", {})
        if current.get("phase") != status["phase"] or current.get("containerStatuses") != status["containerStatuses"] or current.get("conditions") != status["conditions"]:
            self.store.update_status(POD_GVR, runtime.namespace, runtime.name, status, kind="Pod")

    def _container_status(self, state: ContainerState, pod: dict) -> dict:
        container = next((c for c in pod.get("spec", {}).get("containers", []) + pod.get("spec", {}).get("initContainers", [])
                          if c.get("name") == state.name.replace("init:", "")), {})
        if state.terminated:
            status = {"terminated": {"exitCode": state.exit_code, "reason": "Completed" if state.exit_code == 0 else "Error",
                                     "startedAt": state.started_at, "finishedAt": state.terminated_at or now_iso(),
                                     "containerID": f"mini://{uuid.uuid5(uuid.NAMESPACE_DNS, state.name)}"}}
        elif state.started and state.ticks > 0:
            status = {"running": {"startedAt": state.started_at,
                                  "containerID": f"mini://{uuid.uuid5(uuid.NAMESPACE_DNS, state.name)}"}}
        else:
            status = {"waiting": {"reason": "ContainerCreating"}}
        result = {
            "name": state.name.replace("init:", ""),
            "image": container.get("image", ""),
            "imageID": f"mini://{container.get('image', 'unknown')}",
            "ready": state.ready and not state.terminated,
            "started": state.started and not state.terminated,
            "restartCount": state.restart_count,
            "state": status,
            "lastState": deep_copy(state.last_state),
        }
        if state.terminated and state.name.startswith("init:"):
            result["state"] = {"terminated": {"exitCode": state.exit_code, "reason": "Completed",
                                              "startedAt": state.started_at, "finishedAt": now_iso()}}
        return result

    # ------------------------------------------------------------------ helpers
    def _ensure_volumes(self, pod: dict) -> None:
        # No-op: volume authorization is reflected through _missing_volumes.
        return None

    def _missing_volumes(self, pod: dict) -> list[str]:
        missing = []
        namespace = pod["metadata"]["namespace"]
        for volume in pod.get("spec", {}).get("volumes", []):
            claim = volume.get("persistentVolumeClaim", {}).get("claimName")
            if claim:
                pvc = self.store.get(PVC_GVR, namespace, claim)
                if pvc is None or pvc.get("status", {}).get("phase") != "Bound":
                    missing.append(f"pvc/{claim}")
            config_map = volume.get("configMap", {}).get("name")
            if config_map and self.store.get(CONFIGMAP_GVR, namespace, config_map) is None:
                missing.append(f"configmap/{config_map}")
            secret = volume.get("secret", {}).get("secretName")
            if secret and self.store.get(SECRET_GVR, namespace, secret) is None:
                missing.append(f"secret/{secret}")
        return missing

    def _probe(self, pod: dict, container: dict, probe: dict, state: ContainerState) -> bool:
        if int(probe.get("initialDelaySeconds", 0)) > 0 and state.ticks < int(probe["initialDelaySeconds"]):
            return False
        if container.get("name") in ("", None):
            return False
        if self._annotation(pod, "mini-k8s.io/simulate.probe-fail") == "true":
            return False
        if "exec" in probe:
            command = probe["exec"].get("command", [])
            if "false" in command:
                return False
            return True
        if "httpGet" in probe:
            port = probe["httpGet"].get("port")
            if not _declared_port(container, port):
                return False
            return state.ticks >= state.ready_after
        if "tcpSocket" in probe:
            port = probe["tcpSocket"].get("port")
            if not _declared_port(container, port):
                return False
            return state.ticks >= state.ready_after
        if "grpc" in probe:
            return state.ticks >= state.ready_after
        return True

    @staticmethod
    def _annotation(pod: dict, key: str, default: str = "") -> str:
        return pod.get("metadata", {}).get("annotations", {}).get(key, default)

    def _run_ticks(self, pod: dict, container: dict) -> int | None:
        explicit = self._annotation(pod, "mini-k8s.io/simulate.run-ticks")
        if explicit:
            try:
                return int(explicit)
            except ValueError:
                pass
        if pod.get("spec", {}).get("restartPolicy") in ("Never", "OnFailure"):
            # Jobs and one-shot pods finish deterministically.
            return 2
        return None

    def _exit_code(self, pod: dict, container: dict) -> int:
        try:
            return int(self._annotation(pod, "mini-k8s.io/simulate.exit-code", "0"))
        except ValueError:
            return 0

    def _ready_after(self, pod: dict, container: dict) -> int:
        try:
            return int(self._annotation(pod, "mini-k8s.io/simulate.ready-after-ticks", "0"))
        except ValueError:
            return 0

    def _allocate_ip(self) -> str:
        base = self.pod_cidr.rsplit(".", 1)[0]
        self._ip_counter += 1
        return f"{base}.{self._ip_counter}"

    def _append_log(self, state: ContainerState, line: str) -> None:
        state.logs.append(f"{now_iso()} {line}")
        while len(state.logs) > 500:
            state.logs.popleft()

    def _event(self, event_type: str, reason: str, message: str, pod: dict | None) -> None:
        namespace = pod["metadata"].get("namespace", "default") if pod else "kube-system"
        name = f"{pod['metadata']['name']}.{uuid.uuid4().hex[:10]}" if pod else f"kubelet.{uuid.uuid4().hex[:10]}"
        body = {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"name": name, "namespace": namespace},
            "type": event_type, "reason": reason, "message": message,
            "source": {"component": "kubelet", "host": self.node_name},
            "firstTimestamp": now_iso(), "lastTimestamp": now_iso(), "count": 1,
        }
        if pod:
            body["involvedObject"] = {"apiVersion": "v1", "kind": "Pod", "name": pod["metadata"]["name"],
                                      "namespace": namespace, "uid": pod["metadata"].get("uid", "")}
        try:
            self.store.create(EVENT_GVR, body, kind="Event", namespace=namespace)
        except Exception:
            pass

    # --------------------------------------------------------------- node status
    def update_node_status(self) -> None:
        node = self.store.get(NODE_GVR, "", self.node_name)
        if node is None:
            return
        now = now_iso()
        status = deep_copy(node.get("status", {}))
        status["capacity"] = dict(self.capacity)
        status["allocatable"] = dict(self.capacity)
        status["addresses"] = [{"type": "InternalIP", "address": "127.0.0.1"},
                               {"type": "Hostname", "address": self.node_name}]
        status["conditions"] = [
            {"type": "MemoryPressure", "status": "False", "reason": "KubeletHasSufficientMemory",
             "message": "kubelet has sufficient memory available", "lastHeartbeatTime": now,
             "lastTransitionTime": now},
            {"type": "DiskPressure", "status": "False", "reason": "KubeletHasNoDiskPressure",
             "message": "kubelet has no disk pressure", "lastHeartbeatTime": now, "lastTransitionTime": now},
            {"type": "PIDPressure", "status": "False", "reason": "KubeletHasSufficientPID",
             "message": "kubelet has sufficient PID available", "lastHeartbeatTime": now, "lastTransitionTime": now},
            {"type": "Ready", "status": "True", "reason": "KubeletReady",
             "message": "kubelet is posting ready status", "lastHeartbeatTime": now, "lastTransitionTime": now},
        ]
        status["nodeInfo"] = {
            "machineID": "mini-machine", "systemUUID": "mini-system",
            "bootID": "mini-boot", "kernelVersion": "6.8.0-mini", "osImage": "MiniK8s Simulated Linux",
            "containerRuntimeVersion": "mini-runtime://0.9", "kubeletVersion": "v1.31.0-minik8s",
            "kubeProxyVersion": "v1.31.0-minik8s", "operatingSystem": "linux", "architecture": "amd64",
        }
        status["daemonEndpoints"] = {"kubeletEndpoint": {"Port": 10250}}
        status["phase"] = "Running"
        if status != node.get("status"):
            try:
                self.store.update_status(NODE_GVR, "", self.node_name, status, kind="Node")
            except Exception:
                pass

    # -------------------------------------------------------------------- logs
    def logs(self, namespace: str, pod_name: str, container: str | None = None, tail: int = 200,
             previous: bool = False) -> str:
        with self._lock:
            for runtime in self.runtimes.values():
                if runtime.namespace == namespace and runtime.name == pod_name:
                    states = [s for s in runtime.containers.values() if s.name == container]
                    if not states:
                        states = [s for s in runtime.containers.values() if not s.name.startswith("init:")]
                    lines = []
                    for state in states:
                        lines.extend(state.previous_logs if previous else state.logs)
                    return "\n".join(list(lines)[-tail:]) + ("\n" if lines else "")
        return ""

    def exec(self, namespace: str, pod_name: str, container: str | None, command: list[str]) -> dict:
        if not command:
            return {"stdout": "", "stderr": "command is required\n", "exitCode": 1}
        if command[0] == "echo":
            return {"stdout": " ".join(command[1:]) + "\n", "stderr": "", "exitCode": 0}
        if command[0] == "env":
            pod = self.store.get(POD_GVR, namespace, pod_name)
            environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
                           "HOSTNAME": pod_name}
            if pod is not None:
                environment.update(self._container_environment(pod, container))
            stdout = "".join(f"{key}={value}\n" for key, value in environment.items())
            return {"stdout": stdout, "stderr": "", "exitCode": 0}
        if command[0] == "ls":
            return {"stdout": "bin\netc\nhome\nroot\ntmp\nusr\nvar\n", "stderr": "", "exitCode": 0}
        if command[0] == "false":
            return {"stdout": "", "stderr": "", "exitCode": 1}
        return {"stdout": f"simulated exec in {namespace}/{pod_name}: {' '.join(command)}\n", "stderr": "", "exitCode": 0}

    def _container_environment(self, pod: dict, container_name: str | None) -> dict[str, str]:
        """Resolve literal, ConfigMap, Secret and fieldRef env entries.

        The simulation does not start a process, but `exec env` should show the
        same values the kubelet would have injected.  Missing optional refs are
        skipped; missing required refs are rendered as empty so the caller can
        still inspect the Pod (a real kubelet would keep it in CreateContainer
        error instead).
        """
        containers = pod.get("spec", {}).get("containers", [])
        container = next((c for c in containers if c.get("name") == container_name), None)
        if container is None and containers:
            container = containers[0]
        result: dict[str, str] = {}
        for entry in (container or {}).get("env", []) or []:
            name = entry.get("name")
            if not name:
                continue
            if "value" in entry:
                result[name] = str(entry.get("value", ""))
                continue
            source = entry.get("valueFrom") or {}
            value = ""
            config_ref = source.get("configMapKeyRef")
            secret_ref = source.get("secretKeyRef")
            field_ref = source.get("fieldRef")
            if config_ref:
                config_map = self.store.get(CONFIGMAP_GVR, pod["metadata"]["namespace"], config_ref.get("name", ""))
                if config_map is not None:
                    value = str((config_map.get("data") or {}).get(config_ref.get("key", ""), ""))
            elif secret_ref:
                secret = self.store.get(SECRET_GVR, pod["metadata"]["namespace"], secret_ref.get("name", ""))
                if secret is not None:
                    key = secret_ref.get("key", "")
                    if key in (secret.get("stringData") or {}):
                        value = str(secret["stringData"][key])
                    elif key in (secret.get("data") or {}):
                        try:
                            value = base64.b64decode(str(secret["data"][key])).decode("utf-8")
                        except (ValueError, UnicodeDecodeError):
                            value = str(secret["data"][key])
            elif field_ref:
                path = field_ref.get("fieldPath", "")
                if path == "metadata.name":
                    value = pod["metadata"].get("name", "")
                elif path == "metadata.namespace":
                    value = pod["metadata"].get("namespace", "")
                elif path == "spec.nodeName":
                    value = pod.get("spec", {}).get("nodeName", "")
                elif path == "status.podIP":
                    value = pod.get("status", {}).get("podIP", "")
            result[name] = value
        return result


def _declared_port(container: dict, port) -> bool:
    for declared in container.get("ports", []):
        if declared.get("containerPort") == port or declared.get("name") == str(port):
            return True
    return False


def _qos_class(spec: dict) -> str:
    # Init containers count for QoS: a Pod is Guaranteed only if every
    # app *and* init container has CPU/memory requests equal to limits.
    containers = spec.get("containers", []) + spec.get("initContainers", [])
    if not containers:
        return "BestEffort"
    any_resource = False
    guaranteed = True
    for container in containers:
        resources = container.get("resources", {})
        req = resources.get("requests", {})
        lim = resources.get("limits", {})
        if req or lim:
            any_resource = True
        # Guaranteed requires CPU and memory requests == limits for every
        # container.  Setting only CPU (or only one of request/limit) is
        # Burstable, not Guaranteed.
        for key in ("cpu", "memory"):
            if key not in req or key not in lim or parse_quantity(req[key]) != parse_quantity(lim[key]):
                guaranteed = False
        for key in set(req) | set(lim):
            if key not in req or key not in lim or parse_quantity(req[key]) != parse_quantity(lim[key]):
                guaranteed = False
    if not any_resource:
        return "BestEffort"
    return "Guaranteed" if guaranteed else "Burstable"
