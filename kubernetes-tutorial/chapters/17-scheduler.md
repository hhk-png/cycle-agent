# 第 17 章 调度器：把 Pod 放到正确的节点

## 17.1 调度器的职责边界

调度器只做一件事：为未绑定的 Pod 选择节点，并写入 `spec.nodeName`。

它不做：

- 拉镜像、启动容器（kubelet）。
- 保证 Pod 一定能运行（资源、卷、镜像可能仍失败）。
- 重新调度已运行 Pod（节点故障由控制器/descheduler 处理）。
- 修改 Pod 的其他字段。

调度是一个“过滤 + 打分 + 绑定”的过程：

```text
Pod 进入队列
  → 过滤掉不可行节点
  → 对可行节点打分
  → 选最高分（并列随机/稳定策略）
  → 绑定
  → 失败则重试/抢占/报 FailedScheduling
```

## 17.2 调度队列与缓存

真实调度器：

- **ActiveQ**：待调度 Pod。
- **BackoffQ**：失败后指数退避。
- **UnschedulableQ**：暂时不可调度（例如资源不足），集群变化时重新入队。
- **SchedulerCache**：节点、Pod、PVC 的本地缓存，避免每次调度都打 API Server。

Pod 被创建后，API Server watch 事件进入队列。调度器从缓存读取节点和已分配资源，计算可行性，最后通过 Binding 子资源写入。

MiniK8s 没有独立队列/缓存，每轮 list 所有 Pod/Node，按优先级排序处理。这在教学规模下足够，第 40 章说明差距。

## 17.3 过滤（Filter）插件

常见过滤器：

### NodeResourcesFit

检查节点 allocatable 是否满足 Pod requests：

```text
used(cpu) + pod.requests(cpu) <= allocatable(cpu)
used(memory) + pod.requests(memory) <= allocatable(memory)
used(pods) + 1 <= allocatable(pods)
```

requests 计算：

- 主容器求和。
- init containers 取每个资源的最大值。
- 有效请求 = `max(主容器之和, init 最大值)`，不是两者相加。
- 加上 Pod overhead（RuntimeClass）。

### NodeName / NodeSelector

- `spec.nodeName` 直接指定节点，跳过调度器。
- `spec.nodeSelector` 要求节点标签匹配。

### NodeAffinity

- `requiredDuringSchedulingIgnoredDuringExecution`：硬约束，必须满足。
- `preferredDuringSchedulingIgnoredDuringExecution`：软偏好，打分。

支持 `matchExpressions`（In/NotIn/Exists/DoesNotExist/Gt/Lt）和 `matchFields`（metadata.name）。

### TaintToleration

节点污点：

```yaml
taints:
  - key: dedicated
    value: gpu
    effect: NoSchedule
```

Pod 必须容忍：

```yaml
tolerations:
  - key: dedicated
    operator: Equal
    value: gpu
    effect: NoSchedule
```

effect：

- `NoSchedule`：不允许新 Pod 调度。
- `PreferNoSchedule`：尽量不调度（打分惩罚）。
- `NoExecute`：驱逐不容忍的已运行 Pod。

### PodAffinity / PodAntiAffinity

按拓扑域（节点、可用区）约束 Pod 与其他 Pod 的关系：

```yaml
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels: {app: web}
      topologyKey: kubernetes.io/hostname
```

含义：同一节点不能有两个 `app=web` 的 Pod。

注意：

- 亲和性计算依赖已有 Pod 的拓扑分布。
- 大规模下开销大，可能成为调度瓶颈。
- `topologyKey` 节点必须有对应标签，否则行为异常。

### TopologySpreadConstraints

按拓扑域均匀分布：

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels: {app: web}
```

比 podAntiAffinity 更灵活，适合多可用区均匀分布。

### VolumeBinding

检查 PVC 是否已绑定、PV 的节点亲和性是否匹配、StorageClass 是否 WFFC。

WFFC 时，调度器需要先选节点，再触发卷创建；这要求调度器与 CSI provisioner 协作。

### HostPort

同一节点上 hostPort 冲突会阻止调度。

### InterPodAffinity

实现 podAffinity/antiAffinity 的过滤器。

## 17.4 打分（Score）插件

过滤后，对可行节点打分（0–100）：

- **NodeResourcesBalancedAllocation**：CPU/内存使用率越均衡越高。
- **NodeResourcesLeastAllocated**：剩余资源越多越高。
- **NodeAffinity**：满足 preferred 规则加分。
- **InterPodAffinity**：满足软亲和加分，违反软反亲和减分。
- **PodTopologySpread**：分布越均匀越高。
- **ImageLocality**：节点已有镜像加分（减少拉取时间）。
- **TaintToleration**：PreferNoSchedule 减分。
- **NodePreferAvoidPods**：注解标记的节点减分（历史机制）。

最终分数是各插件加权和。权重可配置。

MiniK8s 的 `_score`：

```python
score = 50 * (1 - cpu_used/cpu_alloc)
      + 50 * (1 - mem_used/mem_alloc)
      + preferred_node_affinity
      - prefer_no_schedule_penalty
      - soft_anti_affinity_penalty
```

## 17.5 抢占（Preemption）

当 Pod 无法调度时，调度器可以驱逐低优先级 Pod 腾出资源：

1. 找到“如果移除某些低优先级 Pod 就能容纳”的节点。
2. 选择受害者（尽量少、优先级最低、尽量减少 PDB 违规）。
3. **直接删除受害者 Pod**，不经过 Eviction API；通常使用较短或零 grace period。
4. 高优先级 Pod 重新进入调度。

条件：

- Pod 有 `priority`（通过 PriorityClass）。
- 受害者优先级更低。
- 抢占不保证成功：资源可能被其他 Pod 抢走。

风险：

- 频繁抢占导致抖动。
- 驱逐关键服务（如果优先级设置不当）。
- 抢占可能违反 PDB：PDB 约束的是 Eviction API（drain、自愿驱逐），而抢占走直接删除。调度器在选受害者时会尽量避免 PDB 违规，但 PDB 不是抢占的硬门槛，不能把它当成“抢占保护”。

**真实调度器**：在每个候选节点上尝试“最小受害者集合”，优先选优先级最低、数量最少、对 PDB 影响最小的 Pod；仍然直接删除（不是 Eviction API），所以 PDB 只影响选谁，不是硬性阻止。

**MiniK8s 简化**：`_preempt` 按“优先级最低、创建时间最新”排序，逐个加入受害者前缀，直到剩余资源能容纳高优先级 Pod；前缀内优先级最高/最新的 Pod 可能被多删，不等价于全局最小集合。它还会跳过 `status.disruptionsAllowed < 1` 的 PDB 保护 Pod，并支持 `preemptionPolicy: Never`（包括从 PriorityClass 继承）。测试覆盖了 `Never` 不驱逐、以及低优先级 Pod 被驱逐后高优先级 Pod 得以调度。

## 17.6 PriorityClass

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high
value: 100000
globalDefault: false
preemptionPolicy: PreemptLowerPriority
description: "critical services"
```

- 数值越大优先级越高。
- `globalDefault: true` 作为未指定 Pod 的默认值（只能有一个）。
- `preemptionPolicy: Never` 表示不抢占别人，但仍可被更高优先级抢占。
- 系统关键组件使用 `system-cluster-critical`、`system-node-critical`。

优先级影响：

- 调度顺序。
- 抢占。
- 驱逐顺序（与 QoS 共同作用）。

不要给所有业务高优先级；那等于没有优先级。

## 17.7 调度器配置与多调度器

KubeSchedulerConfiguration：

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
  - schedulerName: default-scheduler
    plugins:
      score:
        disabled:
          - name: ImageLocality
    pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: LeastAllocated
```

可以：

- 启用/禁用插件。
- 调整权重。
- 定义多个 profile。
- 运行多个调度器（`spec.schedulerName` 选择）。

自定义调度器适合特殊硬件/拓扑，但要承担：

- 缓存一致性。
- 高可用。
- 与默认调度器的冲突。
- 维护成本。

多数场景应优先用默认调度器 + 扩展点，而不是重写。

## 17.8 调度器性能

大规模集群的调度瓶颈：

- 节点数量 × Pod 数量的过滤复杂度。
- PodAffinity/TopologySpread 的组合爆炸。
- API Server 写 Binding 的 QPS。
- 调度器缓存与 API Server 不一致。
- 抢占计算昂贵。

优化：

- 使用调度器框架的并行过滤。
- 控制 PodAffinity 的使用。
- 预选/缓存节点快照。
- 分区调度（多个 scheduler profile）。
- 避免频繁创建/删除大量 Pod。
- 使用 `percentageOfNodesToScore` 限制打分节点比例。

Kubernetes 官方性能目标（参考）：5000 节点、15 万 Pod 的规模，调度吞吐需达到一定 SLO。实际取决于工作负载和配置。

## 17.9 常见调度问题

**Pod Pending，事件 FailedScheduling**：

```bash
kubectl describe pod <pod> | grep -A10 Events
```

常见原因：

- 资源不足。
- 节点污点未容忍。
- nodeSelector/affinity 不匹配。
- PVC 未绑定/拓扑不匹配。
- hostPort 冲突。
- 所有节点 NotReady/unschedulable。
- 抢占被禁用或没有合法受害者：`preemptionPolicy: Never`、没有更低优先级的 Pod，或（MiniK8s 中）候选受害者都被 PDB 预算保护。真实调度器的 PDB 只影响受害者选择，不会单独导致 `FailedScheduling`。

**Pod 分布不均**：

- 缺少 topologySpreadConstraints。
- 节点资源差异导致打分偏向。
- HPA 扩缩容后未重新平衡（调度器不会迁移已运行 Pod）。

**调度延迟高**：

- 大规模 PodAffinity。
- 调度器过载。
- API Server 限流。
- 镜像拉取慢（不是调度问题，但表现为启动慢）。

## 17.10 MiniK8s 的实现

`mini_k8s/scheduler/scheduler.py`：

- `schedule_once`：列出 Pending Pod，按优先级/创建时间排序。
- `_fits`：Node Ready、unschedulable、nodeSelector、nodeAffinity、taints、resources、volumes、hostPort、interpod affinity、topology spread。
- `_score`：LeastAllocated + preferred affinity + taint penalty + soft anti-affinity。
- `_preempt`：低优先级驱逐。
- 绑定写 `spec.nodeName`，写 Scheduled 事件。

测试：

- `test_resource_fit_leaves_pod_pending`
- `test_node_selector`
- `test_taint_requires_toleration`
- `test_node_affinity_required`
- `test_interpod_anti_affinity_spreads`
- `test_host_port_conflict`
- `test_preemption_evicts_lower_priority`

## 17.11 调度框架：插件在什么阶段运行

真实调度器不是一个 `filter()` 函数，而是 **Scheduling Framework**。每个调度周期按固定扩展点调用插件：

```text
QueueSort → PreFilter → Filter → PostFilter（失败时）
          → PreScore → Score → NormalizeScore
          → Reserve → Permit → PreBind → Bind → PostBind
```

各阶段职责：

| 扩展点 | 作用 | 典型插件 |
|---|---|---|
| QueueSort | 决定 ActiveQ 中 Pod 顺序 | PrioritySort |
| PreFilter | 预计算/快速失败 | NodeResourcesFit、InterPodAffinity |
| Filter | 排除不可行节点 | TaintToleration、NodeAffinity、VolumeBinding |
| PostFilter | 调度失败后的补救（抢占） | DefaultPreemption |
| PreScore | 生成打分所需状态 | InterPodAffinity、TopologySpread |
| Score | 给节点打分 | LeastAllocated、BalancedAllocation、ImageLocality |
| NormalizeScore | 归一化到 0–100 | 内置打分插件 |
| Reserve | 预留资源（绑定前） | VolumeBinding、NodeResourcesFit |
| Permit | 允许/延迟/拒绝绑定 | gang scheduling 类插件常用 |
| PreBind | 绑定前准备（如卷 attach） | VolumeBinding |
| Bind | 写 Binding | DefaultBinder |
| PostBind | 绑定后清理/通知 | 较少使用 |

几个容易混淆的点：

- **Filter 是并行的**，插件必须无副作用；不要在这里写 API。
- **Reserve 之后失败必须 Unreserve**，否则资源会泄漏到调度器缓存。
- **Permit 是 gang scheduling 的入口**：一组 Pod 可以在此等待，直到全部就绪或超时。
- **PostFilter 只在没有可行节点时运行**，抢占是它的默认实现，不是 Filter 的一部分。
- **打分是加权和**，插件权重可配置；`NodeResourcesBalancedAllocation` 与 `LeastAllocated` 的目标不同（均衡 vs 剩余多）。

### 扩展方式

1. **配置插件**：`KubeSchedulerConfiguration` 启停/调权重，成本最低。
2. **实现插件**：用 Go 写 out-of-tree 插件并编译进调度器（Scheduling Framework）；HTTP extender 是另一套遗留机制，见 17.12.2，不要和 Framework 插件混为一谈。
3. **多调度器**：Pod 指定 `schedulerName`，适合完全不同的调度域，但运维成本高。

生产建议：先用标签/污点/拓扑约束表达需求；确实需要新算法时，优先写插件而不是 fork 调度器。

MiniK8s 的 `_fits`/`_score` 对应 PreFilter+Filter+Score 的简化版，没有 Reserve/Permit，因此无法实现 gang scheduling。真实 Kubernetes 在 1.35 alpha / 1.37 beta 引入了原生 `Workload`/`PodGroup` 与 gang 调度插件；生产上仍常用 Kueue/Volcano 的队列 + gang 组合（第 21 章）。

## 17.12 高级调度：descheduler、原地扩缩容与拓扑细节

### 17.12.1 调度器不迁移已运行的 Pod

调度器只处理“未绑定”的 Pod。以下变化不会触发重新调度：

- 节点负载变化。
- 其他 Pod 被删除，节点变空。
- 拓扑分布变得不均。
- 节点标签/污点变化（已运行 Pod 可能被驱逐，但不会自动重排）。

因此需要 **descheduler**：周期性检查并驱逐/迁移 Pod，让调度器重新放置。常见策略：

- `RemoveDuplicates`：同一节点重复副本。
- `LowNodeUtilization`：节点利用率过低/过高。
- `RemovePodsViolatingInterPodAntiAffinity`：反亲和被破坏。
- `RemovePodsViolatingTopologySpreadConstraint`：分布不均。
- `RemovePodsHavingTooManyRestarts`：频繁重启的 Pod。

descheduler 的代价是“主动扰动”：可能引发滚动、影响 PDB、增加网络/存储压力。生产应：

- 用 dry-run 观察候选。
- 设置 `maxNoOfPodsToEvictPerNode` 等限流。
- 只在低峰运行。
- 与 PDB/优先级配合。

### 17.12.2 调度器扩展器（extender）

Extender 是外置 HTTP 服务，调度器在 Filter/Score/Bind 阶段调用它。它适合：

- 复用已有硬件/网络拓扑系统。
- 用非 Go 语言实现策略。
- 不想改调度器二进制。

代价：

- 每次调度都有网络调用，延迟与可用性成为关键路径。
- 缓存/超时/失败策略要设计；extender 挂掉可能导致全集群无法调度。
- 与调度器缓存的一致性差。

Kubernetes 更推荐 Scheduling Framework 插件；extender 适合过渡/特殊场景。

### 17.12.3 原地扩缩容（in-place resize）

传统扩容要重建 Pod；原地扩缩容在 1.35 GA，允许修改 CPU/内存 requests/limits，由 kubelet 调整 cgroup，不重建 Pod。平台影响：

- 调度器需要感知 Pod 的资源变更，更新缓存与节点账本。
- 缩容可能受节点可分配资源限制而失败，控制器要处理 `PodResizePending`。
- 不是所有资源/运行时都支持；GPU/扩展资源通常不支持原地改，依赖 cgroup v2。
- HPA/VPA 可以用它减少重建，但应用要能处理运行中的 cgroup 变化。

采用前确认 1.35+、运行时/cgroup 支持，以及“缩容失败”时的回退策略；细节见第 7 章 7.7.1 与附录 R。

### 17.12.4 拓扑分布的高级字段

`topologySpreadConstraints` 除了 `maxSkew`/`whenUnsatisfiable`，还有：

- `minDomains`：至少多少个拓扑域参与计算；域太少时防止“把 Pod 全塞进一个域”。
- `nodeAffinityPolicy`：计算分布时是否考虑 nodeAffinity，`Honor`/`Ignore`。
- `nodeTaintsPolicy`：是否考虑污点，`Honor`/`Ignore`。
- `matchLabelKeys`：按 Pod 模板的 label 动态匹配（滚动更新时区分新旧 ReplicaSet）。

这些字段解决“滚动更新期间新旧 Pod 被算作同一组”或“节点亲和导致分布计算偏差”的问题。使用前确认版本支持。

### 17.12.5 调度器 profile 与多调度器

`KubeSchedulerConfiguration` 支持多个 profile：

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
  - schedulerName: default-scheduler
  - schedulerName: batch-scheduler
    plugins:
      score:
        disabled:
          - name: NodeResourcesBalancedAllocation
        enabled:
          - name: NodeResourcesLeastAllocated
```

Pod 通过 `spec.schedulerName` 选择。多调度器的风险：

- 各自缓存不一致，可能重复分配资源。
- 优先级/抢占跨调度器不协调。
- 运维复杂度上升。

多数场景用一个调度器 + 插件配置即可；多调度器适合完全不同的工作负载域（如 HPC vs 在线）。

### 17.12.6 Topology Manager 与 NUMA

调度器决定 Pod 去哪个节点；节点内部，CPU/内存/设备是否在同一个 NUMA 节点，由 kubelet 的 Topology Manager 决定。策略：

- `none`：不协调。
- `best-effort`：尽量对齐。
- `restricted`：允许部分不对齐。
- `single-numa-node`：严格要求同一 NUMA。

对延迟敏感/高性能负载，需要 CPU Manager `static` + Topology Manager + 设备插件配合；配置错误会导致 Pod 无法启动（admission 失败）或性能下降。

### 17.12.7 调度可观测性

关键指标：

- `scheduler_pending_pods`（按 queue）。
- `scheduler_schedule_attempts_total`（result）。
- `scheduler_e2e_scheduling_duration_seconds`。
- `scheduler_framework_extension_point_duration_seconds`。
- `scheduler_preemption_attempts_total`。
- 节点缓存与 API 的偏差。

排障顺序：先看 Pending 原因（事件），再看队列/缓存，最后看插件耗时。不要一上来调 `percentageOfNodesToScore`。

## 17.13 DRA 调度：ResourceClaim、DeviceClass 与设备拓扑

DRA 在 1.34 GA（`resource.k8s.io/v1`）后，设备不再只是 `nvidia.com/gpu: 1` 这样的整数。调度器需要理解 `ResourceClaim`、`DeviceClass`、`ResourceSlice` 和 `ResourceClaimTemplate`：

```text
Pod
 └── spec.resourceClaims[] —— 引用 ResourceClaim（或模板生成的 Claim）
       └── ResourceClaim.spec.devices.requests[]
             ├── deviceClassName: gpu.example.com
             ├── selectors: 显存/型号/拓扑属性
             └── allocationMode: ExactCount / All
ResourceSlice（驱动上报）
 └── 设备列表、属性、节点/PCIe/NUMA 拓扑、健康状态
```

调度流程与扩展资源的关键差异：

1. **Filter**：调度器不只看“节点还剩几个 GPU”，还要看 `ResourceSlice` 里是否有满足属性/拓扑/污点的设备。
2. **Allocate**：调度器在选中节点后把具体设备写入 Claim 的 allocation 结果；这一步是事务性的，失败要回滚。
3. **设备污点**（1.37 GA）：单卡故障可以打 `NoSchedule`/`NoExecute`，调度器像处理节点污点一样避开它；`DeviceTaintRule` 可以批量管理。
4. **共享/分区**：`ResourceClaim` 可以被多个 Pod 共享（组请求），或按可消费容量切分；调度器要计算“剩余容量”而不是“剩余整数”。
5. **优先级备选**（1.36）：一个请求可以列出多个可接受设备，调度器按优先级选择，减少“有卡但型号不匹配”的 Pending。

与 Device Plugin 的对比：

| 维度 | Device Plugin | DRA |
|---|---|---|
| 资源表达 | 整数扩展资源 | 属性、选择器、容量、拓扑 |
| 分配粒度 | 整设备 | 整设备/分区/共享 |
| 拓扑感知 | 靠节点标签/affinity 近似 | 设备级拓扑与 `ResourceSlice` |
| 故障隔离 | 通常靠节点 | 设备污点/驱逐 |
| 生态 | 成熟、广泛 | 1.34 GA，驱动/运行时仍在普及 |

生产建议：新 GPU 平台评估 DRA；存量 Device Plugin 集群不要为了“新”而迁移，先确认驱动、运行时、监控和队列系统都支持。MiniK8s 目前只实现扩展资源的整数账本，DRA 的实现设计见附录 R R.7。

## 本章小结

- 调度 = 过滤 + 打分 + 绑定；调度器不启动容器。
- 资源、污点、亲和性、拓扑、卷是主要约束。
- 抢占是优先级机制的延伸，但有抖动和 PDB 风险。
- 大规模下 PodAffinity/拓扑约束和 API 写放大是主要瓶颈。
- 默认调度器 + 合理配置通常优于自研调度器；真正需要新算法时优先写调度框架插件。
- 调度框架的 PreFilter/Filter/Score/Reserve/Permit/Bind 扩展点决定了插件能力和失败语义。

## 延伸阅读

- [第 7 章 容器运行时、资源与 QoS](07-runtime-resources-qos.md)
- [第 21 章 批处理与队列](21-batch-and-queues.md)
- [第 36 章 调度器与控制器实现](36-scheduler-controllers.md)
