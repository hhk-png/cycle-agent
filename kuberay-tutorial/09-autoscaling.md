# 第 9 章 自动扩缩容：Ray Autoscaler 与 KubeRay

自动扩缩容是 KubeRay 最有价值、也最容易配错的功能。它同时涉及 Ray 的内部调度需求、Kubernetes 的 Pod 调度、以及两者的资源单位对齐。

## 9.1 两层扩缩容

| 层 | 负责者 | 输入 | 输出 |
|---|---|---|---|
| Ray 内部 | autoscaler | 待调度 task/actor 的资源需求、节点利用率 | 需要多少节点 |
| Kubernetes | kube-scheduler | Pod 资源请求、节点容量/亲和性 | Pod 放在哪个节点 |
| KubeRay | operator | RayCluster spec 的 replicas | 创建/删除 Pod |

autoscaler 只改 `replicas`，不直接创建 Pod。operator 仍然是唯一的数据面写入者。

## 9.2 v1 与 v2

| 维度 | Autoscaler v1 | Autoscaler v2 |
|---|---|---|
| 默认版本 | Ray < 2.48 | Ray >= 2.48 |
| 配置 | `rayStartParams.autoscaling-config` | `spec.autoscalerOptions.version: v2` |
| 与 K8s 交互 | 读/写 RayCluster | 使用 KubeRay API + `RAY_CLOUD_INSTANCE_ID` |
| 缩容 | 可能随机删除 | 通过 `workersToDelete` 精确删除 |
| 多 group 优先级 | 有限 | `priority`（Ray >= 2.56） |
| 状态 | 旧 | 新 |

KubeRay 1.4+ 的配置方式：

```yaml
spec:
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
```

不要再用 `RAY_enable_autoscaler_v2` 环境变量与 `autoscalerOptions.version` 同时设置，校验会拒绝。

## 9.3 关键字段

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: autoscaled
spec:
  rayVersion: "2.59.0"
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
    upscalingMode: Default
    idleTimeoutSeconds: 60
    resources:
      requests: {cpu: 500m, memory: 512Mi}
  headGroupSpec:
    rayStartParams: {}
    template:
      spec:
        restartPolicy: Never
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {cpu: "1", memory: 2Gi}
  workerGroupSpecs:
    - groupName: cpu
      replicas: 0
      minReplicas: 0
      maxReplicas: 20
      idleTimeoutSeconds: 60
      priority: 10
      rayStartParams: {}
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: ray
              image: rayproject/ray:2.59.0
              resources:
                requests: {cpu: "4", memory: 8Gi}
    - groupName: gpu
      replicas: 0
      minReplicas: 0
      maxReplicas: 4
      idleTimeoutSeconds: 120
      priority: 0
      rayStartParams: {}
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: ray
              image: rayproject/ray:2.59.0-gpu
              resources:
                requests:
                  cpu: "4"
                  memory: 16Gi
                  nvidia.com/gpu: "1"
                limits:
                  nvidia.com/gpu: "1"
```

### 9.3.1 upscalingMode

| 值 | 行为 |
|---|---|
| `Default` | 按需求直接扩到目标，不额外限速 |
| `Aggressive` | `Default` 的别名，不额外限速 |
| `Conservative` | 限速。v1 的增量上限是 `max(5, current)`（`AUTOSCALER_UPSCALING_INITIAL_NUM_NODES=5`）；v2 在部分 Ray 版本没有严格遵守该模式（上游 issue #50259），以实测为准。MiniKubeRay 教学实现用 `max(1, current)` |

保守模式适合：

- 镜像拉取慢、节点启动慢；
- 担心一次性拉起大量 Pod 打爆 API Server/网络；
- 成本敏感、希望逐步观察。

### 9.3.2 idleTimeoutSeconds

- 全局默认 60 秒；
- worker group 可覆盖；
- 只有“没有使用 Ray 资源”的节点才会被缩容；
- 缩容前要确保没有正在运行的 task/actor。

### 9.3.3 priority

Ray 2.56+ 的 autoscaler v2 支持 group 优先级：

- 高优先级 group 先满足需求；
- 适合“CPU 优先、GPU 兜底”或“便宜机型优先”；
- 不是 Kubernetes 的 PriorityClass，不参与抢占。

### 9.3.4 scaleGate（master/未来）

`scaleStrategy.scaleGate` 是 master 上讨论中的外部扩容门控字段，v1.7.1 发布 API 中没有它。当前可用的是 priority、maxReplicas、批调度器队列/配额。不要把 scaleGate 写进 v1.7.1 生产 YAML。

## 9.4 需求从哪里来

Ray autoscaler 读取：

- 等待资源的 task/actor；
- placement group 的 pending 请求；
- 节点当前利用率；
- 每个节点的资源形状（CPU/GPU/自定义资源/标签）。

它不会读取 Kubernetes 的 Pending Pod。也就是说：

- Ray 里有排队任务 -> 扩容；
- Kubernetes 里 Pod 因资源不足 Pending -> autoscaler 不会因此扩容（它可能继续尝试，但节点容量是集群层面的问题）。

这就是为什么 GPU 集群要同时配置：

- Ray 资源需求；
- 节点池容量/自动伸缩（Cluster Autoscaler/Karpenter）；
- 批调度器队列。

### 9.4.1 需求计算的简化模型

真实 autoscaler 的决策可以简化为：

```text
for each node type (worker group):
    gross_demand = running tasks/actors + pending tasks/actors + placement groups
    remaining = max(0, gross_demand - current_replicas × capacity_per_replica)
    required_extra = ceil(remaining.cpu / node.cpu, remaining.gpu / node.gpu, ...)
    target = clamp(current_replicas + required_extra, minReplicas, maxReplicas)
    if target > current: scale up
    if idle_nodes >= timeout: scale down, never below min
```

口径必须统一：`gross_demand` 是“已运行 + 排队”的总量，因此要减去当前容量；如果你只有“未满足的 pending 请求”，就不要再减当前容量，否则会双重扣除。第 34 章会展开这个公式。

关键输入：

- **pending demand**：Ray 内部排队资源，不是 Kubernetes Pending Pod；
- **node shape**：CPU/GPU/内存/自定义资源/标签；
- **idle time**：节点空闲多久；
- **priority**：多 group 时先满足谁；
- **upscaling mode**：一次扩多少。

MiniKubeRay 的 `AutoscalerPolicy` 就是这个模型的 Python 版（`minikuberay/autoscaler/policy.py`）。

### 9.4.2 autoscaler v2 的运行时契约

v2 sidecar 通过环境变量识别自己管理的节点：

- `RAY_CLOUD_INSTANCE_ID`：Pod 名；
- `RAY_NODE_TYPE_NAME`：worker group 名；
- `RAY_CLUSTER_NAME`/`RAY_CLUSTER_NAMESPACE`：目标 CR。

它 patch：

- `spec.workerGroupSpecs[].replicas`；
- `spec.workerGroupSpecs[].scaleStrategy.workersToDelete`（缩容）。

完整时序（demand → sidecar → patch replicas → Pod → Raylet 注册 → 缩容 victim）见[附录 J.8](appendix-j-sequence-atlas.md)。

因此：

- autoscaler 的 ServiceAccount 需要 patch RayCluster 的权限；
- operator 必须处理 `workersToDelete`，不能随机删；
- 如果 sidecar 没有权限，日志会报 403，但 Ray 任务仍然排队。

### 9.4.3 `minReplicas: 0` 不等于集群缩容到零

`minReplicas: 0` 只把 **worker** 副本降到 0；head Pod、head Service、GCS 仍然存在，仍然占用节点和成本。KubeRay v1.7 没有“整个 RayCluster 自动缩到零”的字段。

要在空闲时真正省钱，需要平台层做：

- 定时/空闲检测后把 `spec.suspend` 设为 `true`（保留 CR，删除 Pod）；
- 或删除 RayCluster/RayJob，按需重建；
- 或用 KEDA/自定义控制器根据队列长度创建/删除 CR。

第 51 章会把这条“Ray 缩容”和“节点缩容”的边界讲完整。

## 9.5 缩容的安全性

autoscaler 缩容时会：

1. 选择空闲超过阈值的节点；
2. 确认没有正在运行的 task/actor；
3. 在 RayCluster 的 `scaleStrategy.workersToDelete` 写入 Pod 名；
4. operator 删除这些 Pod；
5. Raylet 优雅退出。

KubeRay 默认在 autoscaling 开启时**禁止随机删除 Pod**（`ENABLE_RANDOM_POD_DELETE=false`），因为随机删除可能删掉有任务的节点。只有 autoscaler 指定的牺牲者才会被删除。

优雅缩容还需要：

- Raylet 在退出前 drain 本地对象/任务；
- `terminationGracePeriodSeconds` 足够长；
- 有状态 actor 不应被缩容（或先迁移）；
- Serve 副本用 `target_capacity` 控制，不要直接靠节点缩容；
- 监控“缩容后任务重试/失败”。

如果缩容总是删到有任务的节点，先检查：

1. autoscaler 是否真的认为节点 idle；
2. 是否有 actor 持有资源但没上报；
3. `workersToDelete` 是否被错误清空；
4. 是否有其他控制器在改 replicas。

## 9.6 扩容的边界

- `maxReplicas` 是硬上限；
- 节点池没有容量时，Pod 会 Pending；
- GPU 型号/标签不匹配时，Pod 无法调度；
- 镜像拉取时间会影响“扩容看起来没生效”；
- init container 等待 GCS 会增加启动时间；
- 大量并发扩容可能触发 API Server 限流。

## 9.7 资源单位对齐

KubeRay 的 autoscaler 读取的是 Ray 资源，Kubernetes 调度的是 Pod 资源。必须一致：

```yaml
# Pod 请求 4 核
resources:
  requests:
    cpu: "4"
# Ray 看到的也应是 4 核
rayStartParams:
  num-cpus: "4"
```

或使用 KubeRay 1.5+ 的顶层字段：

```yaml
workerGroupSpecs:
  - groupName: cpu
    resources:
      Custom1: "1"
    labels:
      ray.io/zone: us-west-2a
```

顶层字段是推荐写法并会镜像到 Pod 标签；不要与 `rayStartParams` 中的资源字段同时设置（校验会拒绝）。

还有一个隐蔽错配：未显式设置 `num-cpus` 时，KubeRay 通常从 CPU **limit** 推导，而调度器按 **request** 分配。`requests: 4 / limits: 8` 会让 Ray 认为节点有 8 核，实际 K8s 只保证 4 核，扩容/装箱都会算错。建议 requests==limits，或显式设置 `num-cpus` 与 request 对齐。

## 9.8 与 Kubernetes 弹性层的配合

| 组件 | 作用 |
|---|---|
| KubeRay autoscaler | 决定 Ray 需要多少 worker Pod |
| Cluster Autoscaler / Karpenter | 决定节点池是否需要加节点 |
| Kueue / Volcano / YuniKorn | 队列、配额、gang scheduling |
| GPU device plugin | 分配 GPU |
| PriorityClass | Pod 抢占优先级 |

一个完整的弹性链路：

```text
Ray 排队任务
  -> KubeRay autoscaler 扩容 replicas
  -> Pod Pending（节点不足）
  -> Cluster Autoscaler 加节点
  -> kube-scheduler 绑定 Pod
  -> Raylet 注册
  -> 任务开始运行
```

任何一环缺失，都会表现为“扩容没反应”。

## 9.9 调优建议

### 9.9.1 启动时间

- 预拉取 Ray 镜像；
- 使用较小的基础镜像；
- 减少 init container；
- 避免在启动时 pip install 大量依赖（用 runtime env 缓存/镜像预装）；
- GPU 节点预热。

### 9.9.2 缩容时间

- `idleTimeoutSeconds` 太短 -> 抖动；
- 太长 -> 成本高；
- 对有状态 actor 的集群，缩容要更保守；
- Ray Serve 用 `target_capacity` 控制副本，不直接依赖节点缩容。

### 9.9.3 成本

- 多 group 用 priority 优先便宜机型；
- 设置 maxReplicas；
- 用 RayJob 的 `shutdownAfterJobFinishes` + TTL；
- 监控 GPU 利用率；
- 对空闲开发集群设置 suspend 策略。

### 9.9.4 稳定性

- 不要同时开 autoscaler 和手动频繁改 replicas；
- 不要设置 `minReplicas` 过高；
- 不要忽略 Pending Pod；
- 监控 autoscaler 日志；MiniKubeRay 教学指标 `kuberay_autoscaler_actions_total` 不是上游指标，真实集群要看 Ray autoscaler 日志与节点/Pod 指标。

## 9.10 常见故障

| 现象 | 可能原因 | 排查 |
|---|---|---|
| 有排队任务但不扩容 | autoscaling 未开启 / maxReplicas=0 / autoscaler 崩溃 | 看 head autoscaler 日志、`enableInTreeAutoscaling` |
| 扩容后 Pod Pending | 节点容量/GPU/亲和性不足 | `kubectl describe pod` |
| 刚扩容就缩容 | idleTimeout 太短/资源未真正使用 | 看 Ray 利用率、调大 idle |
| 缩容删除了有任务的节点 | 使用了随机删除/旧版 autoscaler | 升级、关闭 `ENABLE_RANDOM_POD_DELETE` |
| 副本数反复抖动 | 需求波动/启动慢/缩容阈值低 | 调大 idle、Conservative、预热 |
| Ray 看到资源与 Pod 不一致 | num-cpus/num-gpus 错配 | 对齐资源 |

## 9.11 MiniKubeRay 的实现

`minikuberay/autoscaler/policy.py` 的 `AutoscalerPolicy`：

1. 读取 `runtime.pending_demand`（CPU 毫核/GPU）；
2. 对每个 group 计算 `required = ceil(demand / capacity)`；
3. 按 priority 排序，从高到低满足需求；
4. 扩容：设置 `replicas`；
5. 缩容：找空闲超过 `idleTimeoutSeconds` 的节点，写入 `workersToDelete`；
6. `Conservative` 限制一次扩容数量；
7. 记录 `kuberay_autoscaler_actions_total` 事件/指标。

测试在 `tests/test_raycluster.py`，演示在 `examples/autoscaling_smoke.py`。

## 9.12 手算：从 demand 到 replicas

在调参之前，先能手算目标副本数。对每个 worker group：

```text
per_replica[r] = 单 Pod 的 r 资源 × numOfHosts
required[r]    = ceil(demand[r] / per_replica[r])
required       = max over r
target         = clamp(required, minReplicas, maxReplicas)
```

例子：单 Pod 8 CPU / 1 GPU / 64Gi，`numOfHosts=1`，pending demand 为 120 CPU、7 GPU、256Gi：

| 资源 | demand | 每副本 | required |
|---|---:|---:|---:|
| CPU | 120 | 8 | 15 |
| GPU | 7 | 1 | 7 |
| Memory | 256Gi | 64Gi | 4 |

`required = 15`；如果 `maxReplicas=10`，目标就是 10：CPU 仍有 40 的缺口继续排队，GPU 已被满足（多出的 3 个 GPU 容量闲置）。这个例子说明：

- `maxReplicas` 是硬上限，不是“建议值”；
- 多资源取最大值，不是求和；
- 多主机要乘 `numOfHosts`；
- 启动时间、镜像拉取、Raylet 注册都要算进端到端扩容时间。

更完整的 v2 内部机制、装箱、缩容 victim 选择和容量模型见[第 34 章](34-autoscaler-v2-internals.md)。

## 9.13 第三个方向：垂直扩容（IPPR）

上面所有决策都只改 `replicas`。KubeRay 在 autoscaler v2 上还提供 In-Place Pod Resizing（IPPR）：把运行中的 worker Pod 的 CPU/memory 调大，而不是新建 Pod。它用 `ray.io/ippr` 注解声明每个 group 的上限，决策顺序是“当前容量 → 最大容量 → resize → 水平扩容”。

IPPR 不是 `upscalingMode` 的替代：`Default/Aggressive/Conservative` 控制水平扩容的步长，IPPR 控制垂直方向的上限。两者可以同时开启，水平路径是垂直上限用尽后的兜底。前置条件（K8s 1.35+、autoscaler v2、不能有 `rayStartParams.num-cpus`、`resizePolicy: NotRequired`）、状态机与生产限制见[第 57 章](57-in-place-pod-resizing-ippr.md)。

## 9.14 小结

- autoscaler 决定“需要多少节点”，operator 决定“如何创建 Pod”。
- v2 是 Ray 2.48+ 的默认；KubeRay 1.4+ 用 `autoscalerOptions.version` 配置。
- upscalingMode、idleTimeout、priority 是 v1.7.1 的三个关键调节点；scaleGate 是未来方向。
- 缩容必须由 autoscaler 指定牺牲者，避免随机删除。
- 资源单位、节点池、批调度器、镜像启动时间都会影响“扩容是否看起来生效”。

下一章进入 RayJob：四种提交模式、状态机、失败重试和清理策略。

→ [第 10 章 RayJob](10-rayjob.md)
