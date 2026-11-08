# 第 2 章 · 核心对象与资源模型

## 2.1 对象全景

Kueue 的对象可以分成四层：

```text
租户入口层
  LocalQueue（namespaced）
        │ 指向
        ▼
配额层
  ClusterQueue（cluster-scoped）
        │ 属于（可选）
        ▼
共享层
  Cohort（cluster-scoped，可层级嵌套）
        │ 引用
        ▼
资源层
  ResourceFlavor（cluster-scoped）
  Topology（cluster-scoped）
  AdmissionCheck（cluster-scoped）

作业层
  Job / JobSet / RayJob / MPIJob / ...
        │ 由集成控制器创建
        ▼
  Workload（namespaced，准入单位）
```

对应关系：

- 一个 namespace 可以有多个 `LocalQueue`。
- 一个 `LocalQueue` 指向一个 `ClusterQueue`。
- 一个 `ClusterQueue` 可以加入一个 `Cohort`（通过 `cohortName`）。
- 一个 `Cohort` 可以有父 Cohort，形成树。
- `ClusterQueue` 的每个资源组引用若干 `ResourceFlavor`，并为每个 Flavor 定义 `nominalQuota`。
- `Workload` 通过 `spec.queueName` 指向同一个 namespace 下的 `LocalQueue`。
- `AdmissionCheck` 由 `ClusterQueue` 引用，在配额预留后执行。

## 2.2 Workload：准入的基本单位

### 2.2.1 它是什么

`Workload` 描述一个"要整体完成的应用"，可以包含一个或多个 `PodSet`。它是 Kueue 的准入单位，也是队列、配额、公平和抢占的计算对象。

Kueue 不直接操作 `batch/v1.Job`。对于 Job，集成控制器会：

1. 读取 Job 的 Pod 模板；
2. 创建对应的 Workload；
3. 暂停 Job（把 `spec.suspend` 设为 true，或使用等价机制）；
4. 等 Workload 被 Admitted 后，再放行 Job。

### 2.2.2 一个 Workload 长什么样

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Workload
metadata:
  name: sample-job
  namespace: team-a
spec:
  active: true
  queueName: team-a-queue
  priority: 0
  podSets:
  - name: driver
    count: 1
    template:
      spec:
        containers:
        - name: driver
          image: example/trainer:latest
          resources:
            requests:
              cpu: "2"
              memory: 8Gi
  - name: worker
    count: 8
    template:
      spec:
        containers:
        - name: worker
          image: example/trainer:latest
          resources:
            requests:
              cpu: "4"
              memory: 16Gi
              nvidia.com/gpu: "1"
```

关键字段：

| 字段 | 含义 |
|---|---|
| `spec.active` | 是否允许排队/继续运行。设为 false 会驱逐正在运行的 Workload，且不会重排队 |
| `spec.queueName` | 目标 LocalQueue 名称（同 namespace） |
| `spec.priority` | 队列/抢占优先级；数值越大优先级越高 |
| `spec.priorityClassRef` | v1beta2 引用优先级类（`group/kind/name`）；`kueue.x-k8s.io/WorkloadPriorityClass` 或 `scheduling.k8s.io/PriorityClass`。可独立于 Pod priority；v1beta1 的 `priorityClassName` 已移除 |
| `spec.podSets` | 一组同构 Pod，包含 `count` 和 Pod 模板 |
| `spec.maximumExecutionTimeSeconds` | 最长运行时间；超时后自动停用 |
| `status.conditions` | `QuotaReserved`、`Admitted`、`Evicted`、`Preempted`、`Finished`、`AdmissionGated` 等；真实 Kueue 里 `Inadmissible` 是 `QuotaReserved=False` 的 Reason，不是独立条件类型（MiniKueue 额外写了合成条件 `Inadmissible=True`，见 2.12） |
| `status.admission` | 配额预留结果：ClusterQueue、Flavor 分配、总用量、Topology 分配 |
| `status.admissionChecks` | 各 AdmissionCheck 的状态 |
| `status.requeueState` | 驱逐后的重排队次数与时间 |
| `status.schedulingStats` | 驱逐统计（按原因） |

### 2.2.3 PodSet 的资源计算

Kueue 的工作量计算方式是：

```text
PodSet 资源 = 单 Pod 请求 × count
Workload 资源 = 所有 PodSet 资源之和
```

例如上面的 Workload：

- CPU：`2×1 + 4×8 = 34`
- 内存：`8Gi×1 + 16Gi×8 = 136Gi`
- GPU：`1×8 = 8`
- Pod 数：`1 + 8 = 9`

Kueue 还会自动把 `pods` 资源计入，用于限制"最多允许多少个 Pod 被准入"。`pods` 是保留资源名，不能出现在 Pod 的 requests 里。

### 2.2.4 请求值的调整与校验

Kueue 计算的不是"YAML 里写了多少"，而是"Pod 实际会请求多少"。它会做三类调整：

1. **LimitRange 默认值**：namespace 配置了默认 requests/limits 时，未写 requests 的 Pod 会按默认值计费。
2. **RuntimeClass overhead**：Pod 的运行时开销会计入请求。
3. **limits 当 requests**：如果只写了 limits 没写 requests，Kueue 用 limits 作为请求值；Pod 级 `spec.resources` 同理。

调整后的值还要通过 LimitRange 校验；不满足时 Workload 会被标记为 `Inadmissible`，而不是简单地"pending 等配额"。这类失败在排障时要先看 LimitRange，而不是先怀疑 ClusterQueue。

另外，Pod 级资源（`pod.spec.resources`，Kubernetes 1.32+ 的 PodLevelResources）也会被计入。多容器 Pod 的 requests 是各容器之和；init container 按 Kubernetes 的 effective request 规则计算（取 init 容器最大值与普通容器之和的较大者），不是简单相加。

### 2.2.5 Workload 的生命周期

```text
创建
  │
  ▼
Pending ──配额足够──► QuotaReserved ──所有 AdmissionCheck Ready──► Admitted
  │                       │                                      │
  │                       │ Rejected/Retry                        │ 完成
  │                       ▼                                      ▼
  │                    Evicted / Pending                     Finished
  │
  └──长期无法满足──► QuotaReserved=False（Reason=Inadmissible）
```

注意：

- `QuotaReserved` 是一个条件，表示逻辑配额已预留，但还不一定能运行。
- `Admitted` 表示所有检查通过，Job 可以放行。
- `Evicted` 表示被抢占、停用、AdmissionCheck 拒绝、超时或 StopPolicy 驱逐。
- `Finished` 表示 Workload 完成，配额释放。

实际判断状态时，不要只看单一条件，而要看组合：

| 观察 | 含义 |
|---|---|
| `QuotaReserved=False` 且没有 `Finished` | Pending；具体原因看 Reason/Message |
| `QuotaReserved=True`、`Admitted=False` | 已预留，在等 AdmissionCheck |
| `Admitted=True`、`Finished=False` | 正在运行 |
| `Finished=True` | 已完成，配额已释放 |
| `Evicted=True` | 曾被驱逐；是否重排队看 `requeueState` 和 `active` |
| `QuotaReserved=False`、`Reason=Inadmissible` | 结构性不可准入（selector、LimitRange 等），需要改配置而不是等配额 |

`status.schedulingStats.evictions` 记录每次驱逐的 `reason` 和 `underlyingCause`。例如 `PodsReadyTimeout` 的 underlyingCause 可能是 `WaitForStart`，自动停用可能是 `Deactivated` + `RequeuingLimitExceeded`。这些字段既是排障线索，也是 `kueue_evicted_workloads_once_total` 指标的去重依据。

### 2.2.6 优先级：Pod priority 与 WorkloadPriorityClass

Kueue 支持两种优先级来源：

1. **Pod PriorityClass**：Kueue 从 Job 的 Pod 模板推断 Workload 的 `spec.priority`。这会影响 Pod 在 `kube-scheduler` 中的优先级。
2. **WorkloadPriorityClass**：只影响 Kueue 的排队和抢占，不影响 Pod 的调度优先级。

Job/JobSet 通过 `kueue.x-k8s.io/priority-class: <name>` 标签选择 WorkloadPriorityClass；v1beta2 的 Workload 用 `spec.priorityClassRef{group,kind,name}` 引用它。标签指向不存在的类时，Workload 创建会失败（事件 `WorkloadPriorityClassNotFound`），不会静默回退到 Pod PriorityClass。详见 9.10。

这让平台可以区分：

- "这个作业对节点调度很重要"（Pod priority）；
- "这个作业在队列里应该先拿到配额"（WorkloadPriority）。

### 2.2.7 停用与重排队

- `spec.active=false`：停用。正在运行的 Workload 被驱逐，不重排队。
- 被抢占：默认会重新排队，可能带指数退避。
- `requeueState.requeueAt`：下次可重排队的时间。
- `maximumExecutionTimeSeconds`：累计 Admitted 时间超过阈值后停用。抢占/重排队只结束当前准入周期，不重置累计时间；删除并重建 Workload（新 UID）才清零，手动 deactivate/activate 的语义按目标版本核对。

### 2.2.8 Admission Gate：配额预留前的闸门

AdmissionCheck 在配额预留**之后**执行；Admission Gate 在配额预留**之前**执行。真实 Kueue 用 Job 上的注解：

```yaml
metadata:
  annotations:
    kueue.x-k8s.io/admission-gated-by: security.example.com/scan,license.example.com/check
```

语义：

- 只要还有 gate，Workload 不会获得 `QuotaReserved`，连配额都不占；
- 外部控制器完成检查后移除自己那一项，Workload 恢复正常排队；
- gate 只能在创建时添加，之后只能移除，不能追加；
- 事件 `AdmissionGated` / `AdmissionGateCleared` 可以用于审计。

它适合"先审批/扫描，再进入配额竞争"的流程。不要把 gate 和 AdmissionCheck 混用：前者防止占用配额，后者防止抢占后无法真正运行。

### 2.2.9 DRA 设备请求如何进入 Workload

DRA 设备不在普通 `requests` 里，Workload 的资源汇总需要显式映射：

```yaml
# Pod 模板（概念示意）
spec:
  resourceClaims:
  - name: gpu
    resourceClaimTemplateName: gpu-a100
```

Kueue 通过 Configuration 的 `deviceClassMappings` 把 `DeviceClass` 映射成逻辑配额资源（例如 `nvidia.com/gpu`），再按设备数/Counter/Capacity 计费。Workload 的 `status.admission` 里看到的是逻辑资源，不是 DeviceClass 本身。排障时要同时看 DeviceClass、映射配置和 Workload 用量，否则容易误判"没有计费"。

MiniKueue 用 `PodSet.Devices[]` + `ClusterQueueSpec.DeviceClassMappings` 建模同一件事：`gpu-a100 -> nvidia.com/gpu`。注意这是**教学简化**：真实 Kueue 的 `deviceClassMappings` 在 `Configuration.resources` 下（元素含 `name` 和 `deviceClassNames`），不在 ClusterQueue spec 里。同一资源如果既写了扩展资源又写了设备请求，按两者较大值计费，避免重复扣费。

## 2.3 LocalQueue：租户入口

`LocalQueue` 是 namespace 级对象，用户只和它打交道。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: team-a-queue
  namespace: team-a
spec:
  clusterQueue: team-a-cq
```

作用：

- 隐藏 ClusterQueue 的集群级细节；
- 让租户可以列出自己可用的队列；
- 作为 Job 的 `kueue.x-k8s.io/queue-name` 标签目标；
- 可以有自己的 `stopPolicy`（暂停该队列的新准入）；
- 可以有自己的 `fairSharing.weight`，用于 Admission Fair Sharing 的历史用量加权（不是 Preemption-based Fair Sharing 的权重）。

`LocalQueue.status` 是很好的排障入口，包含：

| 字段 | 含义 |
|---|---|
| `conditions` | `Active` 条件；LocalQueue 不可用时先看这里 |
| `pendingWorkloads` | 尚未准入的 Workload 数 |
| `reservingWorkloads` | 已预留配额、尚未完成的 Workload 数 |
| `admittedWorkloads` | 已准入、尚未完成的 Workload 数 |
| `flavorsReservation` / `flavorsUsage` | 按 Flavor 统计的预留/使用量 |
| `fairSharing.consumedResources` / `weightedShare` | Admission Fair Sharing 的历史用量与加权份额 |

用户可以这样查看：

```bash
kubectl get localqueues -n team-a -o wide
kubectl get localqueue team-a-queue -n team-a -o jsonpath='{.status}'
# 别名
kubectl get queues -n team-a
```

LocalQueue 的 `stopPolicy` 与 ClusterQueue 的 `stopPolicy` 语义一致，但只影响从该 LocalQueue 提交的 Workload。一个 CQ 下多个 LQ 时，可以只暂停一个租户，而不影响其他租户。

> 注意：LocalQueue 的 `stopPolicy` 和 ClusterQueue 的 `stopPolicy` 会叠加。任一层为 `HoldAndDrain`，该 Workload 都会被驱逐；任一层为 `Hold`，未准入的预留会被取消。

## 2.4 ClusterQueue：配额池

`ClusterQueue` 是集群级对象，定义"这个池子里有多少资源、按什么规则分配"。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-a-cq
spec:
  namespaceSelector: {}          # 允许所有 namespace；null 表示不允许任何 namespace
  cohortName: shared-gpu         # 可选
  queueingStrategy: BestEffortFIFO
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: a100
      resources:
      - name: cpu
        nominalQuota: 128
      - name: memory
        nominalQuota: 1Ti
      - name: nvidia.com/gpu
        nominalQuota: 16
        borrowingLimit: 8
        lendingLimit: 4
  - coveredResources: ["example.com/license"]
    flavors:
    - name: license-pool
      resources:
      - name: example.com/license
        nominalQuota: 10
```

### 2.4.1 资源组（ResourceGroup）

资源组解决的问题是：**哪些资源必须来自同一个 Flavor？**

- 同一组里的资源会一起分配给同一个 Flavor。
- 不同组可以分配不同 Flavor。

例如：

- CPU、内存、GPU 通常在同一组，因为它们都来自同一类节点。
- 网络附加的 license、存储配额可以放在另一组，独立分配。

约束：

- 每个资源只能出现在一个资源组里。
- 每个 Flavor 也只能出现在一个资源组里。
- 资源组最多 16 个；每组最多 64 个资源和 64 个 Flavor；所有资源组加起来最多 256 个资源、256 个 Flavor。
- 每个资源组里的每个 Flavor 必须为组内所有 coveredResources 定义 nominalQuota。

这些数字不是"推荐值"，而是 API 上限。实际配置远低于上限更安全：Flavor 数量直接放大调度器的搜索空间，见第 12 章。

### 2.4.2 每个 Flavor 的配额字段

| 字段 | 含义 | 缺省语义 |
|---|---|---|
| `nominalQuota` | 这个 Flavor 上为该 CQ 保留的配额 | 必填（覆盖资源） |
| `borrowingLimit` | 最多能从同 Cohort 其他 CQ 借多少 | **null = 不限制**（但仍受 Cohort 总量限制） |
| `lendingLimit` | 最多允许别人借走多少自己的空闲配额 | **null = 全部 nominal 都可出借** |

这三个字段是理解 Kueue 配额语义的关键，第 5 章会用大量例子展开。

### 2.4.3 队列策略

| 策略 | 行为 |
|---|---|
| `StrictFIFO` | 按优先级、创建时间排序；队头放不下时，后面的作业也不能插队 |
| `BestEffortFIFO` | 同样排序，但队头放不下时会继续尝试后面的作业 |

默认是 `BestEffortFIFO`。

### 2.4.4 抢占策略

```yaml
spec:
  preemption:
    withinClusterQueue: LowerPriority
    reclaimWithinCohort: LowerPriority
    borrowWithinCohort:
      policy: LowerPriority
      maxPriorityThreshold: 100
```

含义：

- `withinClusterQueue`：同一 CQ 内，是否允许低优先级作业被抢占。
- `reclaimWithinCohort`：当本 CQ 需要收回被别的 CQ 借走的配额时，是否允许抢占那些借用的作业。
- `borrowWithinCohort`：当本 CQ 自己需要借用时，是否允许抢占 Cohort 内的低优先级作业。

细节见第 7 章。

### 2.4.5 停止策略

```yaml
spec:
  stopPolicy: Hold          # None | Hold | HoldAndDrain
```

- `None`：正常。
- `Hold`：不再做新的预留；已准入的继续跑；已预留但未准入的取消预留。
- `HoldAndDrain`：不再做新的预留；已准入的被驱逐，已预留的取消。

这适合维护窗口、配额迁移、紧急止损。

### 2.4.6 namespaceSelector

```yaml
spec:
  namespaceSelector:
    matchLabels:
      team: team-a
```

- `null`（不写）：**不允许任何 namespace**。
- `{}`：允许所有 namespace。
- `matchLabels` / `matchExpressions`：按 Namespace 标签筛选。

这是 Kueue 的基本多租户边界之一，但它不是完整的安全策略。复杂策略应交给 Gatekeeper、Kyverno 或准入控制。

### 2.4.7 准入模式与并发准入

`spec.admissionScope` 决定这个 CQ 是否参与 Admission Fair Sharing。启用 `UsageBasedAdmissionFairSharing` 后，Kueue 会按 LocalQueue 的历史用量排序，而不是只看当前借用。它和 Preemption-based Fair Sharing 是两套机制，详见第 6 章。

`spec.concurrentAdmissionPolicy` 开启 Concurrent Admission（alpha）：Kueue 为 CQ 的每个 Flavor 创建一个 Variant Workload，并行尝试准入；某个 Variant 成功后，Workload 在对应 Flavor 上运行，之后还可以迁移到更优 Flavor。约束是：

- 必须使用 `BestEffortFIFO`；
- 只能有一个 resourceGroup；
- 最多 32 个 Flavor；
- 字段创建后不可变；
- 不支持异构 PodSet（例如 CPU head + GPU worker）。

它用更多的 Workload 对象和调度计算换取更快的放置和迁移能力，见第 9 章。

MiniKueue 实现了 Concurrent Admission-lite：不创建 Parent/Variant 对象，而是让 Workload 先在较后的 Flavor 上准入，后续 tick 若更靠前的 Flavor 变得可行，就驱逐并重新预留到更优 Flavor。它保留了"只向更优迁移、不会来回抖动"的核心不变量，但没有 Variant 的并行准入和 Parent 状态聚合。

### 2.4.8 ClusterQueue 的状态

`ClusterQueue.status` 是排障时最该看的对象之一：

| 字段 | 含义 |
|---|---|
| `conditions` | `Active`、`Ready` 等；`Active=False` 时队列不接纳新 Workload |
| `flavorsReservation` | 每个 Flavor 上已预留的配额（按资源） |
| `flavorsUsage` | 每个 Flavor 上实际使用的配额 |
| `pendingWorkloads` / `reservingWorkloads` / `admittedWorkloads` | 队列计数 |
| `resourceGroups` / `admissionChecks` | 有时会带校验/策略状态，按版本核对 |

注意：`flavorsReservation`/`flavorsUsage` 是 v1beta2 的字段名；旧版本可能用 `flavors[]`。查询脚本不要硬编码路径，先在目标版本上 `kubectl get cq -o yaml` 确认。

## 2.5 ResourceFlavor：异构资源

`ResourceFlavor` 描述一类资源，通常对应一组节点。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100-spot
spec:
  nodeLabels:
    node.kubernetes.io/instance-type: a100-spot
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
```

作用：

1. **配额维度**：ClusterQueue 为每个 Flavor 定义不同配额。
2. **调度约束**：准入后，Kueue 会把 Flavor 的 `nodeLabels` 注入 Pod 的 nodeSelector，把 `tolerations` 注入 Pod，确保 Pod 落到对应节点。
3. **TAS**：`spec.topologyName` 让这个 Flavor 支持拓扑感知调度。

### 空 Flavor

如果集群资源同构，可以只创建一个空 Flavor：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: default-flavor
```

它没有任何标签/污点，适合快速开始。

### 污点与容忍

`ResourceFlavor.spec.nodeTaints` 可以限制哪些 Workload 能使用这个 Flavor。用户需要在 Pod 模板里显式容忍这些污点；Kueue 不会自动为用户注入针对 Flavor 污点的 toleration（与 `spec.tolerations` 的行为不同）。

具体语义：

- 只评估 `NoSchedule` 和 `NoExecute`，忽略 `PreferNoSchedule`；
- 如果 Flavor 同时设置了匹配的 `spec.tolerations`，则该 `nodeTaint` 在准入判断中不再要求用户显式容忍；
- `spec.tolerations` 是**注入**到被准入 Pod 的容忍，不是准入条件；
- `nodeLabels` 最多 8 个，`nodeTaints`/`tolerations` 也各有数量上限。

一个常见误区：以为 `nodeTaints` 会帮用户加 toleration。事实相反：它是"门槛"，用户要自己跨过去；`tolerations` 才是"礼物"，Kueue 帮用户加到 Pod 上。

## 2.6 Cohort：配额共享组

`Cohort` 把多个 ClusterQueue 放在一起，让它们可以互相借用空闲配额。

### 扁平 Cohort

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: shared-gpu
```

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-a-cq
spec:
  cohortName: shared-gpu
  # ...
```

所有 `cohortName: shared-gpu` 的 CQ 属于同一个 Cohort。这里即使不创建 `Cohort` 对象，它们也能互相借用——这叫**隐式 Cohort**。

隐式 Cohort 的能力有限：

- 有借用/出借，但没有 Cohort 级共享配额；
- 不能定义 Cohort 级 borrowingLimit/lendingLimit；
- 不能有父 Cohort；
- 不能设置 Cohort 的公平权重。

一旦你需要共享池、层级或独立权重，就要创建同名 `Cohort` 对象。这时的 Cohort 是**显式 Cohort**，`cohortName` 只是把 CQ 挂到这个名字上。

### 层级 Cohort

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: root
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: org-a
spec:
  parentName: root
  fairSharing:
    weight: "3"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: org-b
spec:
  parentName: root
  fairSharing:
    weight: "1"
```

Cohort 还可以定义自己的 `resourceGroups`/`nominalQuota`，作为树中的共享池。关键语义是：**Cohort 的 nominalQuota 是"额外"的资源，不是把成员 CQ 的配额再汇总一遍。** 一个 Cohort 的可用总量是：

```text
Cohort 总量 = Σ 成员 CQ 的 nominal
            + Cohort 自己的 nominal
            + 从父 Cohort 借入的量（受 borrowingLimit 限制）
```

ClusterQueue 必须为某资源/Flavor 定义 nominalQuota（哪怕为 0），才有资格从 Cohort 借用该资源。这一点经常被忽略：不写 nominalQuota 的 CQ 不能"凭空"借。

Cohort 级字段：

| 字段 | 含义 |
|---|---|
| `spec.parentName` | 父 Cohort；形成树 |
| `spec.resourceGroups` | Cohort 级共享池 |
| `spec.fairSharing.weight` | 同级 Cohort 之间的公平权重 |
| `resourceGroups[*].flavors[*].resources[*].borrowingLimit` | 该 Cohort 从父 Cohort 借入的上限 |
| `resourceGroups[*].flavors[*].resources[*].lendingLimit` | 该 Cohort 向父 Cohort 出借的上限 |

层级 Cohort 的公平分享使用树上的加权 Dominant Resource Share（DRS），并引入 Almost Least Common Ancestor 等概念。第 5、6 章会展开。MiniKueue 已实现配额聚合、Cohort 共享池、隐式 Cohort，以及简化版的 Cohort 级 borrowing/lending limit：borrowingLimit 作为子树借用总量的上限，lendingLimit 作为子树出借能力的上限逐个成员收紧。它仍未实现 AlmostLCA 的层级比较。

## 2.7 AdmissionCheck：准入的外部闸门

`AdmissionCheck` 是集群级对象，表示"配额预留后还要满足的条件"。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: prov-test
spec:
  controllerName: kueue.x-k8s.io/provisioning-request
  parameters:
    apiGroup: kueue.x-k8s.io
    kind: ProvisioningRequestConfig
    name: prov-test-config
```

状态：

| 状态 | 含义 |
|---|---|
| `Pending` | 还没结果 |
| `Ready` | 通过 |
| `Retry` | 暂时失败，释放配额并退避重试 |
| `Rejected` | 永久失败，停用 Workload |

`status.admissionChecks[*]` 还有三个关键字段：

| 字段 | 含义 |
|---|---|
| `requeueAfterSeconds` | `Retry` 后至少等待多久再重试；不填则下次调度立即重试 |
| `retryCount` | 进入 `Retry` 的次数，由 Kueue 自动递增 |
| `podSetUpdates` | 检查控制器建议的 Pod 模板修改，例如注入 annotation |

`podSetUpdates` 是 ProvisioningRequest 与 Cluster Autoscaler 协作的关键：扩容控制器可以告诉 Job 集成"在 Pod 上加这个 annotation"，让自动扩缩器识别该使用哪个 ProvisioningRequest。

典型用法：

- **ProvisioningRequest**：等云 autoscaler 扩出节点。
- **MultiKueue**：等某个 worker 集群接受 Workload。
- **自定义控制器**：检查许可证、配额、审批流、安全扫描等。

## 2.8 Topology：物理拓扑

`Topology` 描述节点层级，例如：

```text
block
  └── rack
        └── hostname
```

配合 `ResourceFlavor.spec.topologyName` 和 Pod 模板注解，Kueue 可以做拓扑感知调度（TAS），尽量把需要高带宽通信的 Pod 放到同一个 rack/block。第 9 章详细讲。

## 2.9 对象关系图

```text
Namespace team-a
  LocalQueue team-a-queue ──────────────┐
  Workload job-1 ──queueName────────────┘
       │
       │ admitted into
       ▼
  ClusterQueue team-a-cq ──cohortName──► Cohort shared-gpu ◄── Cohort root...
       │                                   ▲
       │ resourceGroups                    │
       ▼                                   │
  Flavor a100-spot ──topologyName──► Topology gpu-topology
       │
       └── nodeLabels/tolerations ──► Pods ──► kube-scheduler ──► Nodes

  AdmissionCheck prov-test ◄── referenced by ClusterQueue
```

## 2.10 从静态配额到动态容量

`ClusterQueue.spec.resourceGroups` 是静态配额，但 Kueue 还在演进动态配额编排（DQO，alpha）：

```text
CapacityProvider（实际可用容量）
        │
        ▼
DynamicQuotaOrchestrator（按 nominal 比例分配）
        │
        ▼
ClusterQueue.status.effectiveQuotas（生效配额）
        │
        ▼
Scheduler 使用 effectiveQuotas 做准入
```

这带来一个重要的心智模型：**你写的 nominalQuota 不一定就是调度器实际使用的配额。** 如果集群启用了 DQO，实际生效的是 `effectiveQuotas`；排障时要同时看 spec 和 status。

DQO 的几个关键细节：

- **两阶段**：Discovery 汇总 CapacityProvider 的容量（可乘以 `effectiveCapacityMultiplier`）；Distribution 按 nominal 比例分给子树，写入 `status.effectiveQuotas`。
- **只影响被编排的 Flavor**：没有列在 `spec.orchestratedFlavors` 的 Flavor 继续用 spec。
- **borrowingLimit/lendingLimit 从 spec 复制**；CQ 的 lendingLimit 会被 effective nominal 截断。
- **capacityDistribution 可以省略**：只做 Discovery，不改变配额，适合先观察"如果按实际容量分会怎样"。
- **容量下降不会驱逐已准入 Workload**：这是 alpha 的已知限制，排障时会看到 usage 暂时高于 effectiveQuota。
- **effectiveQuotas 可能残留**：删除 orchestrator、改子树根或分发失败时，旧值会留在对象上，Kueue 继续按旧值准入，直到有人清理。

MiniKueue 用 `CapacityProvider` + `EffectiveQuotas` 模拟了 Discovery + Distribution 的主干（见第 5、13 章），但没有实现条件、优先级冲突、容量下降驱逐和残留清理。

## 2.11 Workload 的更新、字段归属与并发

Workload 是 Kueue 与 Job 集成共同读写的对象，字段归属很重要：

- Job 集成创建 `spec`（podSets、queueName、priority 等），并负责在 Workload 不存在时重建；
- Kueue 写 `status`（conditions、admission、admissionChecks、requeueState、schedulingStats；reclaimablePods 由集成写）；
- 普通用户通常只读 Workload；不要手工改已准入 Workload 的资源请求，否则账本会漂移。

Kueue 默认用 Server-Side Apply（SSA）更新 Workload status。SSA 的字段所有权可以避免多个控制器互相覆盖，但它对重复 key/value 有已知限制。需要 Merge Patch 行为时，可以启用 alpha 的 `WorkloadRequestUseMergePatch` gate。MultiKueue 的 External Dispatcher 也依赖这个 gate 来修改 `status.nominatedClusterNames`。

这意味着两件事：

1. 排障时不要只看 `kubectl get workload -o yaml` 的最终值，还要看 `metadata.managedFields`，确认是谁写的。
2. 自定义集成不要和 Kueue 争夺同一字段；用 ownerReferences 和确定性名称保证幂等。

## 2.12 MiniKueue 对象扩展

MiniKueue 的 API 不是 CRD，但它刻意保留了与本章对象一一对应的字段，新增能力也遵循同一套语义：

| 真实 Kueue | MiniKueue 字段/对象 | 场景 |
|---|---|---|
| `deviceClassMappings` | `ClusterQueueSpec.DeviceClassMappings`、`PodSet.Devices` | `dra` |
| `admission-gated-by` 注解 | `WorkloadSpec.AdmissionGates` | `admission-gate` |
| `admissionScope` + LocalQueue weight | `ClusterQueueSpec.AdmissionScope`、`LocalQueue.FairSharing`、`Engine.AFS` | `admission-fair-sharing` |
| `concurrentAdmissionPolicy` | `ClusterQueueSpec.ConcurrentAdmission`、`Admission` 的 Flavor 迁移 | `concurrent-admission` |
| MultiKueue manager/worker | `internal/multikueue` 包 | `multikueue` |
| Cohort 级 borrowing/lending limit | `Cohort.ResourceGroups[].Flavors[].BorrowingLimit/LendingLimit` | `hierarchical-limits` |
| 隐式 Cohort | 只有 `cohortName`、没有 Cohort 对象 | `implicit-cohort` |
| 显式 Cohort 共享池 | `Cohort.ResourceGroups` 的独立 nominal | `cohort-shared-pool` |
| TAS slice/spreading | `PodSetTopology.SliceSize` / `Spreading` | `tas-slice` |
| `resources.transformations` | `ResourceTransformation`、`WorkloadSpec.ResourceTransformations` | `resource-transform` |
| ProvisioningRequest | `controller.ProvisioningConfig` + AdmissionCheck 状态机 | `provisioning-request` |
| `PreemptionConfig` | `api.PreemptionConfig`、`ClusterQueueSpec.PreemptionConfigName` | `configurable-preemption` |
| `fairSharing.preemptionStrategies` | `Engine.FairSharingPreemption` | `fair-sharing-preemption` |
| 指数退避 | `controller.BackoffConfig` | `retry-backoff` / 抢占 |
| `QuotaReserved=False/Reason=Inadmissible` | 额外写合成条件 `Inadmissible=True`，供 Visibility 查询 | `label-selector` 等 |

这些字段是**教学语义**，不是可直接提交给 Kubernetes 的 CRD；真实字段名和默认值以第 24 章的迁移表和官方 API reference 为准。

## 2.13 本章小结

- `Workload` 是准入单位，包含一个或多个 `PodSet`；资源按 `count × request` 汇总，并会按 LimitRange/overhead/limits 做调整。
- `LocalQueue` 是租户入口，`ClusterQueue` 是配额池，`Cohort` 让 CQ 之间借用配额；隐式 Cohort 只有借用，显式 Cohort 才有共享池/层级/权重。
- `ResourceFlavor` 表达异构资源，通过 nodeLabels 约束、tolerations 注入影响真实调度；nodeTaints 是用户必须自行容忍的门槛。
- `AdmissionCheck` 把云扩容、多集群、拓扑等外部条件接入两阶段准入，支持 requeueAfterSeconds/retryCount/podSetUpdates。
- `namespaceSelector` 的 `null` 与 `{}` 语义不同，且支持 matchLabels 与 matchExpressions。
- DQO 启用时，调度器使用 `status.effectiveQuotas`，排障要同时看 spec 与 status。
- Workload 的 spec/status 有明确归属，默认用 SSA 更新；不要手工改已准入的资源请求。
- Admission Gate 在配额预留之前，AdmissionCheck 在预留之后；DRA 设备通过映射进入逻辑配额。
- MiniKueue 已覆盖 DRA-lite、资源变换、ProvisioningRequest-lite、Configurable Preemption-lite、Fair Sharing 抢占-lite、admission gate、AFS-lite、Concurrent Admission-lite、MultiKueue-lite、Cohort 级 limit、隐式 Cohort、显式 Cohort 共享池和 TAS slice/spreading。
- 下一章会把这些对象放进一个可运行的最小集群示例。

---

[← 上一章：为什么需要 Kueue](01-why-kueue.md) | [返回目录](../README.md) | [下一章：安装与快速上手 →](03-install-and-quickstart.md)
