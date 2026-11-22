# 第 7 章 容器运行时、资源与 QoS

## 7.1 镜像不是容器

- **镜像（image）**：只读的文件系统层 + 配置（entrypoint、env、user），由 OCI Image Spec 定义。
- **容器（container）**：镜像 + 可写层 + namespace/cgroup 隔离 + 进程。
- **Pod**：一组共享网络/存储/生命周期的容器。

Kubernetes 通过 CRI 调用运行时：

```text
kubelet ── CRI gRPC ──▶ containerd / CRI-O ── OCI runtime ──▶ runc/crun
```

CRI 定义：

- `RuntimeService`：RunPodSandbox、CreateContainer、StartContainer、StopContainer、ExecSync、PortForward。
- `ImageService`：PullImage、ListImages、RemoveImage、ImageStatus。

Pod 启动顺序大致：

1. 创建 sandbox（持有 network namespace、Pod IP）。
2. 配置 CNI 网络。
3. 拉取镜像。
4. 启动 init containers。
5. 启动主容器。
6. 执行探针。

MiniK8s 的 `mini_k8s/kubelet/kubelet.py` 用 `PodRuntime` 和 `ContainerState` 模拟这套状态机，不调用 CRI。模块边界与真实 kubelet 类似：watch Pod → 确保 sandbox/容器 → 写 status。

## 7.2 镜像拉取策略

```yaml
imagePullPolicy: IfNotPresent   # 默认（非 latest）
imagePullPolicy: Always         # latest 标签默认
imagePullPolicy: Never          # 只用本地镜像
```

生产建议：

- 不要用 `latest`；使用不可变 tag（Git SHA、semver）或 digest。
- 私有镜像通过 imagePullSecrets 或节点级凭据。
- 大镜像影响冷启动和节点磁盘；使用多阶段构建、精简基础镜像。
- 注意镜像仓库限流（Docker Hub、云厂商 ACR/ECR/GAR）和跨区流量成本。
- 供应链安全：签名、SBOM、漏洞扫描、admission 校验 digest。

## 7.3 requests 与 limits 的真实语义

```yaml
resources:
  requests:
    cpu: 250m
    memory: 256Mi
    ephemeral-storage: 1Gi
  limits:
    cpu: "1"
    memory: 512Mi
    ephemeral-storage: 2Gi
```

### CPU

- `requests.cpu`：调度器用来判断节点是否有足够 CPU；也是 cgroup `cpu.shares` 的权重参考。
- `limits.cpu`：cgroup CPU quota，超过会被**节流（throttle）**，不会杀进程。
- CPU 是压缩资源（compressible）：争抢时按权重分配，不会 OOM。

单位：

- `1` = 1 核。
- `500m` = 0.5 核。
- `0.1` = 100m。
- 精度到 1m；小于 1m 会被取整。

### 内存

- `requests.memory`：调度依据；cgroup `memory.low`/权重参考。
- `limits.memory`：cgroup 硬限制，超过触发 **OOM Kill**，容器退出，restartPolicy 决定是否重启。
- 内存是不可压缩资源。

### 省略 requests 时会发生什么

这是资源管理里最容易被说错的一点。Kubernetes 的默认行为不是“requests 缺失就按 0 算”，而是**从 limits 反向补齐 requests**（除非 LimitRange 明确设置了 `defaultRequest`）：

| 写法 | 写入 API 后的 requests | 调度器记账 | QoS |
|---|---|---|---|
| 只有 `limits.cpu: "1"` | `requests.cpu: "1"` | 按 1 核预留 | 若 CPU/内存都如此，Guaranteed |
| 只有 `requests.cpu: "1"` | 仍是 1 核，没有 limit | 按 1 核预留 | Burstable（无上限） |
| `requests: 500m` + `limits: 1` | 500m / 1 | 按 500m 预留 | Burstable |

因此“只写 limits”并不会超卖，反而会把整个 limit 预留给该 Pod，降低节点可调度密度；真正按 0 计算的是 requests 和 limits **都不写**（BestEffort）。生产上要显式写 requests（按 P95 用量），再决定 limit 是等于 request（可预测、Guaranteed）还是更高（允许突发、Burstable）。

扩展资源（如 `nvidia.com/gpu`）更严格：必须是整数，且 request 必须等于 limit；只写 limit 会补 request，只写 request 会补 limit，但两者不相等会被准入拒绝。

常见错误：

- 只设 limits 不设 requests：API Server 会把 request 默认成 limit，调度按 limit 预留、QoS 变 Guaranteed；容量规划会误以为“没预留”。
- requests 远低于实际：节点看起来有空闲，实际内存压力大。
- limits 等于 requests 但应用有突发：CPU 被节流，延迟上升。
- Java/Go 应用不识别 cgroup 限制：JVM 堆按宿主机内存设置，容器被 OOM。需要 `-XX:MaxRAMPercentage` 或 `GOMEMLIMIT`。

### ephemeral-storage

容器可写层、日志、emptyDir 占用节点磁盘。超过 limit 会被驱逐；节点磁盘压力也会触发驱逐。生产要设置 requests/limits，并监控节点 disk pressure。

### hugepages

`hugepages-2Mi`、`hugepages-1Gi` 是独立资源，必须显式请求，通常配合 `hugetlb` cgroup。数据库、DPDK 类应用会用到。

## 7.4 QoS 类别

调度器与 kubelet 根据 requests/limits 计算：

| QoS | 条件 | 驱逐优先级 |
|---|---|---|
| Guaranteed | 每个容器都有 CPU/内存 requests 且等于 limits（只写 limits 时 request 会被默认成 limit，因此也可能成为 Guaranteed） | 最后 |
| Burstable | 至少一个容器有 requests/limits，但不满足 Guaranteed | 中间 |
| BestEffort | 所有容器都没有 requests/limits | 最先 |

注意：

- QoS 只看 CPU/内存，不看 ephemeral-storage。
- init containers 也参与计算。
- Guaranteed 不等于“不会被驱逐”；节点故障、维护仍会驱逐。
- QoS 影响 kubelet 的 OOM score 和驱逐顺序，但不是调度器的打分项。

MiniK8s 的 `_qos_class` 实现了简化判定；测试 `test_qos_class_burstable_and_guaranteed` 验证。

## 7.5 节点可分配资源

节点容量（capacity）不等于可分配（allocatable）：

```text
Allocatable = Capacity
            - kube-reserved        # kubelet、容器运行时、系统守护进程
            - system-reserved      # 操作系统、sshd、日志
            - eviction-threshold   # 为驱逐预留的缓冲
```

`kubectl describe node` 会显示 capacity 与 allocatable。调度器只用 allocatable。

生产建议：

- 为系统组件设置 `--kube-reserved`、`--system-reserved`、`--eviction-hard`。
- 不要把所有内存都给 Pod；节点需要缓冲，否则系统 OOM 会杀 kubelet/容器运行时。
- 不同实例类型、不同工作负载用不同 NodePool。

## 7.6 驱逐与 OOM

节点资源压力时，kubelet 触发驱逐：

- `memory.available` 低于阈值。
- `nodefs.available`、`imagefs.available` 低于阈值。
- `pid.available` 不足。

驱逐顺序：

1. BestEffort / 低优先级。
2. 使用超过 requests 的 Burstable。
3. Guaranteed。
4. 仍不足则系统 OOM。

Pod 被驱逐时 phase 变为 Failed，reason `Evicted`，事件包含资源原因。控制器会重建。

OOM Kill 与驱逐不同：

- OOM 是容器超过 memory limit，内核杀死进程。
- 驱逐是节点整体资源不足，kubelet 主动删 Pod。

排查：

```bash
kubectl describe pod <pod> | grep -A5 "Last State"
kubectl get events --field-selector reason=Evicted
dmesg | grep -i oom
```

## 7.7 CPU 节流

CPU limit 通过 cgroup CFS quota 实现：每 100ms 周期内只能用一定配额。突发流量可能在一个周期内用完配额，然后被 throttle 到下一个周期。

症状：

- P99 延迟周期性尖刺。
- `container_cpu_cfs_throttled_seconds_total` 上升。
- 应用 CPU 使用率看起来不高，但延迟高。

解决：

- 提高 CPU limit 或去掉 limit（保留 requests）。
- 调整应用并发/批处理。
- 使用 HPA/VPA 或节点扩容。
- 对延迟敏感服务，考虑 CPU manager static 策略 + Guaranteed QoS + 独占核。

### 7.7.1 原地调整 Pod 资源（In-place Pod Resize）

1.35 起，原地扩缩容 GA：通过 Pod 的 `resize` 子资源修改运行中容器的 CPU/内存 requests/limits，不重建 Pod。它和“改 Deployment 模板触发滚动更新”是两条完全不同的路径。

```yaml
spec:
  containers:
    - name: app
      resources:
        requests: {cpu: "500m", memory: "512Mi"}
        limits:   {cpu: "1",    memory: "1Gi"}
      resizePolicy:
        - resourceName: cpu
          restartPolicy: NotRequired
        - resourceName: memory
          restartPolicy: NotRequired
```

要点：

- 只支持 CPU/内存（以及部分版本的容器级资源），不支持 GPU 等扩展资源；依赖 cgroup v2。
- `resizePolicy` 决定某类资源变化时是否必须重启容器；`NotRequired` 也不保证一定成功。
- 1.34 起用 `PodResizePending` / `PodResizeInProgress` conditions 表达进度；排障先看 conditions，不要只看 Pod phase。
- 缩容内存 limit 可能触发 OOM 竞争；先压测，再纳入变更管理。
- VPA 的 `InPlaceOrRecreate` 可以优先走原地调整，失败再重建；HPA 仍只改副本数。两者同时工作时要有防抖和优先级。

完整时间线与限制见[附录 R](../appendices/R-version-evolution-1.32-1.37.md) R.4.2。

## 7.8 CPU Manager 与拓扑管理

默认 CPU 是共享的。对低延迟、高性能应用：

- **CPU Manager static policy**：Guaranteed Pod 的整数核可以独占。
- **Topology Manager**：协调 CPU、内存、设备（GPU/NIC）的 NUMA 亲和。
- **Memory Manager**：NUMA 感知内存分配。

这些需要 kubelet 配置和特定硬件，普通业务通常不需要。

## 7.9 RuntimeClass 与沙箱

RuntimeClass 让不同 Pod 使用不同运行时：

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata
handler: kata
overhead:
  podFixed:            # 每个 Pod 的固定开销，调度时计入
    cpu: 250m
    memory: 128Mi
scheduling:
  nodeSelector: {runtime: kata}
  tolerations:
    - {key: runtime, operator: Equal, value: kata, effect: NoSchedule}
---
apiVersion: v1
kind: Pod
metadata:
  name: sandboxed
spec:
  runtimeClassName: kata
  containers:
    - name: app
      image: nginx
```

三个字段要分清：

- `handler`：CRI 中注册的运行时名；节点必须真的配置了该 handler，否则 Pod 启动失败。
- `overhead.podFixed`：沙箱/VM 自身的固定资源开销。调度器把它加到 Pod 的 requests 上，避免“容器装得下、加上 VM 就装不下”。
- `scheduling`：使用该 RuntimeClass 的 Pod 默认附加的 nodeSelector/tolerations，用来把 Pod 引导到装有对应运行时的节点。它不是强制隔离，节点标签仍需正确维护。

常见运行时：

- **runc**：默认，性能好，隔离依赖 namespace/cgroup。
- **gVisor**：用户态内核，syscall 拦截，隔离更强，性能开销更大。
- **Kata Containers**：轻量 VM，强隔离，启动慢。
- **Firecracker**：microVM，常用于 serverless。
- **WASM（runwasi）**：WASM 工作负载，启动快、体积小，生态仍在发展。

选择取决于威胁模型、性能预算和运维能力。

### 7.9.1 CRI 之下：containerd、shim、OCI 与 snapshotter

kubelet 不直接调用 runc。以 containerd 为例，一次容器启动大致经过：

```text
kubelet
  └─ CRI gRPC ──▶ containerd（daemon）
                    ├─ 拉取/解压镜像（content store + snapshotter）
                    ├─ 创建 sandbox（pause 容器，持有 netns）
                    ├─ 创建容器任务（containerd-shim）
                    └─ shim ──▶ runc（OCI runtime）──▶ 容器进程
```

几个容易混淆的层次：

- **CRI**：kubelet 与运行时的接口，面向 Pod/容器，不理解 Dockerfile。
- **containerd**：运行时守护进程，管理镜像、快照、容器生命周期、CRI 插件。
- **containerd-shim**：每个容器一个（或按运行时不同），在 containerd 重启时保持容器进程存活，并负责回收退出码。
- **OCI runtime（runc/crun）**：按 OCI Runtime Spec 创建 namespace/cgroup、挂载 rootfs、exec 进程，创建完就退出。
- **snapshotter（overlayfs、native、devmapper）**：管理镜像层与容器可写层。overlayfs 最常见；devmapper 提供块设备级隔离但更重。

Pod 的 sandbox 很关键：pause 容器持有 network namespace，其他容器通过 `shareProcessNamespace`/network namespace 加入。sandbox 重建会导致 Pod IP 变化，因此 kubelet 会先重建网络再重建容器。

镜像路径：

```text
registry → content store（按 digest 存 blob）
          → snapshotter 解压为可挂载快照
          → 容器 rootfs = 只读层 + 可写层
```

因此“镜像有多大”与“节点磁盘占用”不是一回事：同一镜像的多个容器共享只读层，但每个容器有自己的可写层。镜像 GC 与容器可写层 GC 是两套逻辑。

### 7.9.2 CRI 的流式接口

`kubectl logs`、`kubectl exec`、`kubectl port-forward` 不是简单的 HTTP 代理：

- kubelet 通过 CRI 的 streaming server 获取日志/建立 exec 流。
- exec 使用 SPDY/WebSocket 升级，stdin/stdout/stderr 多路复用。
- 日志由运行时写入节点文件（如 `/var/log/pods/...`），kubelet 读取并支持 `--previous`。
- 容器退出后日志仍在，直到被轮转/清理；`kubectl logs` 读的是文件，不是运行时内存。

排障命令：

```bash
# 直接问 CRI（绕过 kubelet）
crictl pods
crictl ps -a
crictl inspect <container-id>
crictl logs <container-id>

# containerd 层
ctr -n k8s.io containers list
ctr -n k8s.io tasks list
ctr -n k8s.io images list

# OCI/runtime 层
runc list
systemd-cgls /kubepods.slice
```

注意：`crictl` 看到的是节点上的真实状态；`kubectl get pods` 看到的是 API Server 中的期望/观测状态。两者不一致时，先判断是 kubelet 没上报、运行时没执行，还是 API 写入失败。

### 7.9.3 镜像与容器 GC

节点磁盘是有限资源。kubelet 有两类回收：

- **镜像 GC**：当镜像占用超过 `imageGCHighThresholdPercent`，删除未使用镜像，直到低于 `imageGCLowThresholdPercent`。
- **容器/日志 GC**：删除已退出容器、轮转日志；`containerLogMaxSize`/`containerLogMaxFiles` 控制单个容器日志。

驱逐（eviction）与 GC 不同：

- GC 回收“不再需要”的对象。
- Eviction 在节点资源压力下驱逐运行中的 Pod，按 QoS/优先级排序。

常见事故：日志没轮转把磁盘写满 → kubelet 进入 DiskPressure → 驱逐 Pod → 业务抖动。容器日志应同时配置 kubelet 轮转和日志采集端的限流。

### 7.9.4 运行时故障模式

| 现象 | 可能原因 | 排查 |
|---|---|---|
| Pod 一直 ContainerCreating | 镜像拉取慢、CNI 失败、卷挂载失败 | `kubectl describe pod` 事件、`crictl ps -a`、节点 kubelet 日志 |
| `failed to create sandbox` | CNI 插件错误、netns 残留 | CNI 日志、`crictl pods`、重启运行时 |
| `OCI runtime create failed` | cgroup/namespace/权限、SELinux | `dmesg`、runc 错误、节点安全策略 |
| 容器反复重启但日志为空 | entrypoint 退出、镜像架构不匹配 | `crictl inspect`、`docker manifest inspect` |
| 节点 NotReady，运行时不可用 | containerd 挂起、磁盘满、shim 泄漏 | `systemctl status containerd`、`crictl info`、磁盘/inode |
| exec 失败但容器 Running | streaming server/网络、RBAC | kubelet 端口、`kubectl auth can-i create pods/exec` |

运行时的健康直接影响节点状态。生产节点应监控 containerd 的 gRPC 延迟、shim 数量、镜像存储占用和 `crictl info` 的运行时条件。

## 7.10 镜像与节点存储

节点磁盘被以下内容占用：

- 镜像层。
- 容器可写层。
- 容器日志。
- emptyDir。
- 系统日志、kubelet 数据。

kubelet 有 image GC 和 container GC 阈值。生产要：

- 设置日志轮转（containerLogMaxSize、containerLogMaxFiles）。
- 监控 imagefs。
- 避免在容器内写大量临时文件。
- 大镜像预拉取（DaemonSet 或镜像缓存）。

## 7.11 资源治理实战

### 设置合理的 requests

没有通用公式。可以从监控开始：

1. 先只设 requests，观察实际 CPU/内存 P95。
2. requests 略高于 P95（例如 1.2 倍），避免调度过紧。
3. 内存 limit 设为 P99 + 缓冲，或暂时不设 limit（风险是节点 OOM）。
4. 用 VPA recommendation 辅助，但不要盲目自动应用。

### LimitRange 默认值

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: defaults
  namespace: team-a
spec:
  limits:
    - type: Container
      defaultRequest: {cpu: 100m, memory: 128Mi}
      default: {cpu: 500m, memory: 512Mi}
```

防止用户忘记设置资源。MiniK8s 在准入阶段注入，测试见 `test_limit_range_defaults`。

### ResourceQuota

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: compute
  namespace: team-a
spec:
  hard:
    requests.cpu: "10"
    requests.memory: 20Gi
    limits.cpu: "20"
    limits.memory: 40Gi
    pods: "50"
    count/deployments.apps: "20"
```

配额是命名空间级的总量限制，不是单 Pod 限制。第 26 章展开。

## 7.12 MiniK8s 中的资源

调度器 `pod_requests` 计算：

- 主容器求和。
- init containers 取每个资源的最大值。
- 有效请求 = `max(主容器之和, init 最大值)`，不是两者相加。
- 与节点 `allocatable` 比较。

```python
def pod_requests(pod):
    app = {}
    for c in pod.spec.containers:
        app[key] = app.get(key, 0) + requests[c]
    init = {}
    for c in pod.spec.initContainers:
        init[key] = max(init.get(key, 0), requests[c])
    return {k: max(app.get(k, 0), init.get(k, 0))
            for k in set(app) | set(init)}
```

这个模型与真实调度器一致（忽略 overhead、hugepages 细节）。

QoS 计算见 `mini_k8s/kubelet/kubelet.py::_qos_class`。

## 7.13 cgroups v2、systemd 与节点运行时配置

requests/limits 最终落到 cgroup。现代发行版逐步切换到 cgroups v2，理解差异可以解释很多“资源限制不生效”的问题。

### cgroup v1 与 v2

| 维度 | v1 | v2 |
|---|---|---|
| 层级 | 每种资源一棵树（cpu、memory…） | 统一层级 |
| CPU 权重 | `cpu.shares` | `cpu.weight` |
| CPU 限额 | `cpu.cfs_quota_us`/`period` | `cpu.max` |
| 内存上限 | `memory.limit_in_bytes` | `memory.max` |
| 内存保护 | `memory.soft_limit_in_bytes` | `memory.low`/`memory.min` |
| 压力指标 | 有限 | PSI（`cpu.pressure`、`memory.pressure`、`io.pressure`） |

Kubernetes 在 v2 下：

- requests 影响 `cpu.weight` 和内存保护。
- limits 写入 `cpu.max`/`memory.max`。
- `memory.high` 不是 Kubernetes 的 limit，但可用于更平滑的节流（通常由运行时/系统调优）。
- PSI 让“节点/容器是否饱和”更容易观测，适合排查延迟抖动。

### systemd cgroup driver

containerd/CRI-O 与 kubelet 必须使用同一个 cgroup driver。生产推荐 **systemd**：

```toml
# /etc/containerd/config.toml（containerd 2.x，config version 3）
version = 3

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]
  SystemdCgroup = true
```

containerd 1.x 的路径是 `[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]`；2.x 把 CRI 插件拆成 `io.containerd.cri.v1.runtime` 与 `io.containerd.cri.v1.images`，旧配置在 2.x 上不会生效。升级运行时前先确认 kubelet 与 containerd 的 cgroup driver 一致。

```yaml
# kubelet 配置
cgroupDriver: systemd
```

不一致的典型症状：

- kubelet 启动失败，日志提示 cgroup 层级不匹配。
- Pod 创建后资源限制不生效或重复创建 cgroup。
- 节点重启后容器被 systemd 清理。

### 节点调优不是“越大越好”

- 开启 cgroup v2 需要内核/发行版/运行时都支持；老内核或某些 GPU 驱动可能不兼容。
- `kube-reserved`/`system-reserved` 要留出运行时、kubelet、日志的空间；否则节点 OOM 会先杀系统进程。
- 对延迟敏感服务，CPU manager static + topology manager 可以减少跨 NUMA 抖动，但会降低调度灵活性。
- **swap 默认 `NoSwap`，但不是永久禁止**：NodeSwap 在 1.34 GA，节点可通过 kubelet 的 `memorySwap.swapBehavior: LimitedSwap` 启用受限 swap（依赖 cgroup v2）。调度器仍不感知 swap，容量规划仍按物理内存；启用前要验证 QoS/驱逐和延迟。

MiniK8s 只做资源数量的加减，不涉及 cgroup；这些配置在真实节点上决定“limits 是否真的生效”。

## 7.14 多架构与 Windows 节点

Kubernetes 集群不要求所有节点同构，但“混合架构”会引入调度、镜像和运行时差异。

### 多架构镜像

ARM64 节点（Graviton、Ampere、鲲鹏等）越来越常见。镜像必须是多架构 manifest：

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t registry.example.com/app:1.0 --push .
```

检查：

```bash
docker manifest inspect registry.example.com/app:1.0
kubectl get nodes -L kubernetes.io/arch
```

调度：

```yaml
nodeSelector:
  kubernetes.io/arch: arm64
```

或使用 `nodeAffinity` 让工作负载优先/必须落在某架构。混合架构集群里，**没有多架构镜像的 Pod 会在错误架构节点上 ImagePull/exec 失败**，而不是调度失败。

### Windows 节点

Kubernetes 支持 Windows 节点，但差异很大：

- Windows 节点只能运行 Windows 容器；Linux 容器需要 Linux 节点（反之亦然）。
- 容器运行时是 containerd/CRI，不是 Docker；Windows 容器版本必须与节点 OS 版本兼容。
- 不支持 Linux capabilities、seccomp、AppArmor；securityContext 的很多字段无效。
- 网络依赖 CNI（如 Calico Windows、Antrea Windows）；NetworkPolicy 支持程度取决于 CNI。
- 部分 Linux 专属特性（hostPID、privileged、某些卷类型）不可用。
- 调度要用 `nodeSelector: kubernetes.io/os: windows`，并注意 `kubernetes.io/os` 标签。

混合集群的建议：

- 用节点池/taints 隔离 OS/架构，避免误调度。
- 用 multi-arch 镜像 + 显式 nodeSelector。
- CI 至少在目标架构上跑一次冒烟测试。
- 不要把“在 amd64 Linux 上能跑”当成跨平台保证。

MiniK8s 不区分 OS/架构；它把所有节点当作同构执行环境。真实集群的资源模型必须把 `kubernetes.io/os`、`kubernetes.io/arch` 纳入调度与镜像策略。

## 本章小结

- 镜像是只读模板，容器是运行实例，Pod 是共享网络/存储的容器组。
- requests 决定调度，limits 决定运行时上限；CPU 超限被节流，内存超限被 OOM。
- QoS 影响驱逐顺序，不改变调度可行性。
- 节点 allocatable 小于 capacity；系统预留和驱逐阈值必须配置。
- RuntimeClass 提供不同隔离级别，选择取决于威胁模型与性能。
- cgroup v2/systemd driver 决定 limits 是否真正生效；多架构镜像与 Windows 节点需要显式调度和镜像策略。

## 延伸阅读

- [第 6 章 Pod 与生命周期](06-pod-lifecycle.md)
- [第 17 章 调度器](17-scheduler.md)
- [第 26 章 多租户与资源治理](26-multitenancy-governance.md)
- [第 49 章 应用工程](49-application-engineering.md)：语言运行时内存配置、并发/过载与容量模型。
- [附录 P 字段语义、默认值与 YAML 陷阱](../appendices/P-defaults-and-yaml-traps.md)：requests/limits/QoS 的默认化与常见配置错误。
- [附录 T Windows 容器与混合操作系统集群](../appendices/T-windows-and-mixed-os.md)：Windows 隔离模型、资源语义与节点差异。
