# 第 37 章 Kubelet 与运行时模拟

## 37.1 kubelet 的职责

真实 kubelet：

- 注册 Node、上报心跳/资源。
- watch 绑定到本节点的 Pod。
- 通过 CRI 创建/删除容器。
- 通过 CNI 配置网络。
- 通过 CSI 挂载卷。
- 执行探针。
- 收集日志/指标。
- 管理容器重启。
- 驱逐资源压力下的 Pod。

MiniK8s 的 kubelet 保留前四项的可观测契约，跳过真实 CRI/CNI/CSI。

## 37.2 数据结构

```python
@dataclass
class ContainerState:
    name: str
    requested_ticks: int | None
    ticks: int
    started: bool
    ready: bool
    terminated: bool
    exit_code: int
    restart_count: int
    logs: deque[str]
    previous_logs: deque[str]
    ready_after: int
    probe_ok: bool

@dataclass
class PodRuntime:
    uid: str
    namespace: str
    name: str
    node_name: str
    pod_ip: str
    init_done: bool
    containers: dict[str, ContainerState]
    phase: str
```

每个 kubelet 按节点维护 `runtimes: dict[uid, PodRuntime]`。节点容量 `capacity` 除 CPU/内存/Pod 数外，还可以包含 `nvidia.com/gpu` 这类扩展资源；真实 kubelet 从 Device Plugin 的 `ListAndWatch` 获得，MiniK8s 在创建节点时静态注入（见第 22 章与示例 16）。

## 37.3 reconcile 循环

```python
def reconcile(self):
    pods = [p for p in store.list(pods) if p.spec.nodeName == self.node_name]
    for pod in pods:
        if deleted/terminal: continue
        self._reconcile_pod(pod)
    cleanup runtime for pods no longer present
```

`_reconcile_pod`：

1. 创建/获取 PodRuntime，分配 Pod IP。
2. 检查卷依赖（ConfigMap/Secret/PVC）。
3. 运行 init containers（模拟一次性完成）。
4. 创建/推进容器。
5. 计算 Pod phase。
6. 写 status。

## 37.4 容器状态机

```text
未创建
  → started, ticks=1
    → ready = ready_after <= ticks and probe_ok
    → 如果 requested_ticks 到达: terminated(exit_code)
      → restartPolicy Always/OnFailure: restart
      → Never: Pod Succeeded/Failed
```

关键点：

- `run-ticks` 决定容器运行多久。
- 默认 Job/一次性 Pod 为 2 ticks。
- 长驻容器没有 requested_ticks，保持 Running。
- 重启保留 `previous_logs`，`restartCount` 累加。
- `ready_after` 控制就绪延迟。

所有模拟开关都放在 Pod 注解里，便于 YAML 实验与测试：

| 注解 | 类型/默认 | 作用 | 读取方 |
|---|---|---|---|
| `mini-k8s.io/simulate.run-ticks` | int；`Never`/`OnFailure` 默认 2，其余不设置 | 容器运行多少个 tick 后退出 | kubelet |
| `mini-k8s.io/simulate.exit-code` | int；默认 0 | 退出码，决定 Succeeded/Failed 与重启 | kubelet |
| `mini-k8s.io/simulate.ready-after-ticks` | int；默认 0 | 启动后多少 tick 变为 Ready | kubelet 探针 |
| `mini-k8s.io/simulate.probe-fail` | `"true"` | 让探针失败，用于观察重启/NotReady | kubelet 探针 |
| `mini-k8s.io/simulate.cpu-usage` | quantity；默认 requests 的 60% | PodMetrics 的 CPU 用量 | MetricsController |
| `mini-k8s.io/simulate.memory-usage` | quantity；默认 requests 的 70% | PodMetrics 的内存用量 | MetricsController |

这些不是 Kubernetes API 的一部分；换到真实集群前必须删除，否则只是无害的注解，但不会产生模拟效果。

## 37.5 Init containers

```python
def _run_init_containers(pod, runtime, containers):
    for c in containers:
        state = ContainerState(name=f"init:{c.name}")
        state.terminated = True
        state.exit_code = 0
    runtime.init_done = True
```

所有 init containers 在一个 tick 内完成。真实 kubelet 会顺序执行，失败重试。

`status.initContainerStatuses` 写入 terminated 状态。

## 37.6 探针

### Readiness

```python
readiness = container.readinessProbe
if readiness:
    ok = self._probe(...)
    state.probe_ok = ok
state.ready = state.probe_ok and state.ticks >= state.ready_after
```

`_probe`：

- `exec`：命令含 `false` → 失败。
- `httpGet`/`tcpSocket`：端口必须在 container ports 中声明；且 ticks >= ready_after。
- `grpc`：按 ready_after。
- `initialDelaySeconds`：未到时间返回 False。
- `mini-k8s.io/simulate.probe-fail: "true"`：强制失败。

### Liveness

```python
if probe fails:
    liveness_failures += 1
    if >= failureThreshold:
        terminated = True
        exit_code = 137
        event Unhealthy
```

下一轮按 restartPolicy 重启。

### Startup

```python
if startupProbe and not startup_ok:
    if probe fails and failures >= failureThreshold:
        terminate(exit 137); event Unhealthy
    startup_ok = probe succeeds
    ready = startup_ok and ticks >= ready_after
    # liveness/readiness are skipped while starting
```

启动探针成功前，readiness 保持 False，liveness 不执行；失败超过阈值则重启容器。这解决了“慢启动应用被 liveness 反复杀死”的问题。测试 `test_startup_probe_failure_restarts_container` 覆盖。

真实探针会真正发 HTTP/TCP/exec，并受 timeoutSeconds/periodSeconds 控制。MiniK8s 用 tick 近似。

## 37.7 Pod phase 计算

```python
if not init_done: Pending
if all containers terminated:
    if all exit 0 and restartPolicy in (Never, OnFailure): Succeeded
    if any nonzero and restartPolicy == Never: Failed
if any started: Running
else: Pending
```

注意：

- `Always` 的容器终止后会重启，所以不会进入 Succeeded。
- 多容器 Pod 需要所有容器都成功才 Succeeded。
- 一个容器失败且 Never → Failed。

## 37.8 status 写入

```python
status = {
  "phase": runtime.phase,
  "hostIP": "127.0.0.1",
  "podIP": runtime.pod_ip,
  "startTime": ...,
  "conditions": [PodScheduled, Initialized, ContainersReady, Ready],
  "containerStatuses": [...],
  "initContainerStatuses": [...],
  "qosClass": ...
}
store.update_status(...)
```

只在 phase/containerStatuses/conditions 变化时写，减少 RV churn。

MiniK8s 还写 `podIPs`、`hostIP`、`startTime`、`qosClass`。真实 kubelet 还会写：

- `observedGeneration`。
- `resize` 状态（原地扩缩容）。
- `ephemeralContainerStatuses`。
- `hostIPs`。

## 37.9 卷检查

```python
def _missing_volumes(pod):
    for volume:
        PVC: must exist and Bound
        ConfigMap: must exist
        Secret: must exist
    return missing
```

缺失时：

- phase Pending。
- message `container runtime: waiting for volumes: ...`。
- 不启动容器。

真实 kubelet 会发 FailedMount 事件，并重试挂载。

## 37.10 日志

每个 ContainerState 有：

- `logs`：当前容器实例日志。
- `previous_logs`：上一次实例日志。

`logs()`：

- 按 namespace/pod 查找 runtime。
- 选择 container。
- 返回最后 tail 行。
- `previous=True` 返回 previous_logs。

真实日志来自 CRI/节点文件，支持 follow、timestamps、since、tail。

## 37.11 exec

```python
def exec(namespace, pod, container, command):
    if command[0] == "echo": return stdout
    if command[0] == "env": return resolved_env
    if command[0] == "ls": return listing
    if command[0] == "false": return exit 1
    return simulated output
```

`env` 会读取 Pod spec 的 `env`，解析字面值、`configMapKeyRef`、`secretKeyRef`（含 base64 `data` 与 `stringData`）和 `fieldRef`（metadata.name/namespace、spec.nodeName、status.podIP）。**叠加顺序是：先放 `PATH`/`HOSTNAME` 默认值，再应用 Pod 的 `env` 条目**，所以 Pod 显式设置的 `PATH`/`HOSTNAME` 会覆盖默认值（与真实容器运行时一致）。这让 ConfigMap/Secret 示例可以被断言，而不是只验证对象存在。

API Server 的 `/exec` 返回 Kubernetes Status，含 stdout/stderr。

真实 exec 通过 CRI ExecSync/Exec，支持 TTY、stdin、resize。

## 37.12 Node 心跳

`update_node_status`：

- capacity/allocatable。
- addresses。
- conditions：MemoryPressure/DiskPressure/PIDPressure/Ready。
- nodeInfo。
- daemonEndpoints。
- lastHeartbeatTime。

每 0.5 秒更新。NodeLifecycleController 检查心跳，超过 grace 标记 NotReady 并加 taint。

MiniK8s 的节点 IP 固定 `127.0.0.1`，Pod IP 从 `10.244.<node>.0/24` 分配。

## 37.13 删除与终止

- Pod 有 deletionTimestamp 时，kubelet 不再推进，标记 runtime.deleted。
- 真实 kubelet 会执行 preStop、SIGTERM、grace、SIGKILL。
- MiniK8s 立即停止推进；对象不是由 `GarbageCollector` 删除的。`ResourceStore.update/delete` 在最后一个 finalizer 被清空时完成物理删除并发出 DELETED 事件；GC 只处理“ownerReferences 指向的所有者已不存在”的孤儿对象。两者不要混为一谈。

没有实现：

- 优雅终止。
- preStop。
- 镜像拉取/GC。
- 资源压力驱逐。
- 静态 Pod。
- 原生 sidecar。

## 37.14 为什么模拟而不是执行

- 安全：不执行用户命令。
- 确定性：测试可重复。
- 零依赖：CI 不需要 Docker。
- 可观察：状态机清晰。

代价：

- 不能验证真实镜像/命令。
- 不能测真实网络/存储。
- 不能替代 e2e。

## 37.15 测试覆盖

- Pod 运行/IP/QoS。
- readiness 延迟/探针端口。
- liveness 重启。
- Job 完成/失败。
- init containers。
- 缺失 ConfigMap 阻塞。
- 日志/previous 与 `lastState`。
- exec。
- QoS 类别：init 容器计入；Guaranteed 要求**每个容器**的 CPU 与内存 requests 都等于 limits（只设 CPU 相等、内存不设仍是 Burstable）；BestEffort 是两者都不设。
- startupProbe 失败重启。
- 终止 Pod 删除后清理 terminal runtime。

测试文件：`tests/test_kubelet.py`。

## 37.16 真实 kubelet 的内部管理器

MiniK8s 用一个 `reconcile` 循环覆盖了 kubelet 的主路径；真实 kubelet 则是十几个管理器围绕 `syncLoop` 协作。理解这些边界，才能看懂“Pod 卡在 ContainerCreating”“节点 NotReady”“磁盘压力驱逐”等现象。

### 37.16.1 syncLoop 与 pod worker

真实 kubelet 的主循环大致是：

```text
API watch / config / file source
        │  PodUpdate
        ▼
     syncLoop
        │  按 Pod UID 分发
        ▼
   pod worker（每个 Pod 一个 goroutine）
        ├─ syncPod：计算期望 vs 实际
        ├─ 创建/删除 sandbox、容器
        ├─ 挂载/卸载卷
        └─ 更新 status
```

关键设计：

- **按 Pod 串行**：同一 Pod 的多次变更不会并发执行，避免重复创建容器。
- **期望状态来自 API**：kubelet 不“记住命令”，而是每次重新计算。
- **重试与退避**：失败不会丢事件，会按 backoff 重试。
- **Pod worker 生命周期**：Pod 删除后 worker 要等清理完成才退出。

MiniK8s 的顺序循环等价于“全局串行”，行为更确定，但没有 per-Pod worker 的并发与隔离。

### 37.16.2 PLEG：Pod Lifecycle Event Generator

kubelet 需要知道容器何时启动/退出。它不会为每个容器阻塞等待，而是：

- 周期性（默认 1s）向 CRI 查询所有 Pod/容器状态。
- 比较缓存，生成“容器启动/退出/死亡”事件。
- 唤醒对应的 pod worker。

PLEG 的 `relist` 延迟是节点健康的重要指标：

- `kubelet_pleg_relist_duration_seconds` 高 → 运行时慢/节点过载。
- PLEG 不健康 → kubelet 可能报告 NodeNotReady，即使容器还在跑。

这解释了一个常见现象：容器进程正常，但节点 NotReady，原因是 kubelet 与运行时之间的状态同步卡住。

### 37.16.3 status manager

kubelet 不直接写 API 的 Pod status，而是通过 status manager：

- 合并来自 pod worker、probe manager、runtime 的状态。
- 控制写入频率，避免每个探针都打 API。
- 处理 API 冲突/重试。
- 保证 `status.observedGeneration` 与 spec 版本对应。

MiniK8s 的 `update_status` 直接写 store，简单但会产生更多写入；真实实现会做批处理与去重。

### 37.16.4 volume manager

volume manager 负责：

- 等待卷 attach/mount（由 CSI/云控制器完成 attach，kubelet 完成 mount）。
- 处理 `VolumeAttachment`、`NodeStageVolume`、`NodePublishVolume`。
- 重试失败挂载，报告 `FailedMount` 事件。
- 卸载不再使用的卷（需处理仍被容器占用的情况）。
- 卷健康监控（部分 CSI 驱动支持）。

Pod 卡在 `ContainerCreating` 且事件是 `FailedMount` 时，问题通常在 CSI/存储/节点，而不是容器运行时。

### 37.16.5 image manager 与 container GC

- image manager 拉取镜像、记录使用情况、触发镜像 GC。
- container GC 删除已退出容器与旧日志。
- 二者都要避免“刚删完又被拉取”的抖动，因此有 high/low watermark。

与 eviction 的区别见 7.9.3。

### 37.16.6 eviction manager

当节点出现 MemoryPressure/DiskPressure/PIDPressure 时，eviction manager 按优先级驱逐 Pod：

1. 先看 QoS：BestEffort → Burstable → Guaranteed。
2. 同 QoS 内看优先级、资源使用是否超过 requests。
3. 驱逐是“软/硬”阈值组合，给系统留出回收时间。

这就是为什么：

- 没有 requests 的 Pod 最先被驱逐。
- Guaranteed Pod 不是“永不驱逐”，只是最后。
- 磁盘/inode 耗尽也会触发驱逐，即使内存充足。

### 37.16.7 cgroup manager 与拓扑管理器

- cgroup manager 把 Pod/容器放到 cgroup v2 层级，设置 CPU/内存/IO 限制。
- CPU manager 的 `static` 策略为 Guaranteed Pod 分配独占 CPU，减少抖动。
- Topology manager 让 CPU/设备/内存的 NUMA 节点对齐，影响高性能负载。
- memory manager 管理巨页与 NUMA 内存绑定。

这些能力依赖 kubelet 配置、运行时和节点特性；不是所有托管集群都允许调整。

### 37.16.8 节点生命周期与优雅操作

真实 kubelet 还负责：

- **节点状态上报**：capacity/allocatable、conditions、images、nodeInfo。
- **证书轮转**：TLS bootstrap + `--rotate-certificates`。
- **优雅节点关闭**：检测系统关机，终止 Pod 并上报，避免“突然消失”。
- **静态 Pod**：从本地 manifest 启动，API 中只读镜像 Pod。
- **Device Plugin**：注册/分配设备，处理设备健康。
- **Node Problem Detector**（独立 DaemonSet）：把内核/硬件问题写成 Node condition/Event。

这些是“节点运维”而不是“Pod 编排”，但在故障排查中同等重要。

### 37.16.9 与 MiniK8s 的映射

| 真实组件 | MiniK8s 对应 | 差距 |
|---|---|---|
| syncLoop + pod worker | `Kubelet.reconcile` | 无 per-Pod 并发/退避 |
| PLEG | 直接读 runtime 状态 | 无 relist 延迟/事件 |
| status manager | `update_status` | 无合并/限流 |
| volume manager | `_missing_volumes` | 无 attach/mount/CSI |
| image/container GC | 无 | 不拉镜像/不占磁盘 |
| eviction manager | 无 | 无资源压力驱逐 |
| cgroup/CPU/topology manager | QoS 分类 | 无真实 cgroup/NUMA |
| 证书轮转/优雅关闭 | 无 | 单进程模拟 |

MiniK8s 的价值是暴露“kubelet 如何把期望状态变成容器状态”的骨架；真实节点的故障往往发生在上述管理器之间，而不是在 `syncPod` 的主路径上。

## 本章小结

- MiniK8s kubelet 用确定性状态机模拟容器生命周期、探针、日志和 exec。
- 它保留 Pod phase、conditions、containerStatuses 等可观测契约。
- 真实 kubelet 还负责 CRI/CNI/CSI、优雅终止、驱逐、静态 Pod。
- 模拟适合控制面学习和 CI，不替代真实集群测试。

## 延伸阅读

- [第 6 章 Pod 与生命周期](06-pod-lifecycle.md)
- [第 7 章 容器运行时、资源与 QoS](07-runtime-resources-qos.md)
- [第 40 章 与真实 Kubernetes 的差距](40-gap-and-replacement.md)
